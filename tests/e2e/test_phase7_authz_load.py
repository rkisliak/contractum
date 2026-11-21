"""The authz load profile (D-187): deep grant sets, a 251-group principal, calibrated ceilings.

Seeding and traffic run on the backend network only (``internal("identity")``,
``internal("audit")``), never through Traefik: the harness measures the PDP, the denial writes
and the audit query, not the gateway's rate limiter.

Seeding (concurrency 8, per-run names):

* ``DC_E2E_LOAD_ORGS`` (default 50) Organizations x 3 Projects x 3 Teams (650 folders by default);
* about 200 noise principals (local users and ROOT-owned local groups) holding about
  ``DC_E2E_LOAD_GRANTS`` (default 5 000) role assignments at every level;
* carol, the Entra persona in 251 directory groups (the overage path through the fake Graph):
  the "Data Eng" group is registered (409 reuse), and her generated filler groups get a display
  name in the fake Graph, are registered and are granted on per-run folders only: ``org_reader``
  at every "visible" Organization (one group each) plus ``team_admin`` at a few Teams. The last
  tenth of the Organizations are "foreign": carol holds nothing there. The fake Graph's
  membership is never mutated (that would cost the 60 s directory-cache wait); its display names
  are reset at the end.

Workload: ``DC_E2E_LOAD_REQUESTS`` (default 3 000) requests at ``DC_E2E_LOAD_CONCURRENCY``
(default 25), shuffled:

* 60 % identity internal checks as carol, a third each: ALLOW (``contract:read`` at a Team of a
  visible Organization, inherited from a group grant), NOT_FOUND (``contract:read`` at a Team of a
  foreign Organization) and FORBIDDEN (``contract:create`` at a Team she can read only);
* 25 % ``accessible-folders`` for ``contract:read`` as carol (several hundred folder ids);
* 15 % audit queries, half as an Org admin, half as carol (her ``audit:query`` scope).

Every check carries its own UUID4 ``X-Request-ID``. Gates: zero 5xx and zero transport errors,
every check answers its expected outcome, every denied check appears exactly once in the audit
query by request id and no allowed check appears there, and the p95 of each operation is below
its ceiling. p50/p95 per operation and the sizes used are printed and written to
``/dev-out/phase7-load.json`` (the host's ``.dev/phase7-load.json``).

Ceilings (env ``DC_E2E_LOAD_P95_CHECK_MS``, ``DC_E2E_LOAD_P95_FOLDERS_MS``,
``DC_E2E_LOAD_P95_AUDIT_MS``; empty means the default) are generous on purpose: correctness is the
real gate, and the ceilings only catch pathological regressions. Each default is the larger of
five times the first measured p95 and the research default (500 / 1 000 / 1 500 ms).

Calibration (2026-11-14, default sizes, warm stack; MacBook Pro, Intel Core i9-9880H, 8 cores /
16 threads, Docker Desktop VM with 16 CPUs and 16 GB, one uvicorn worker per service):

* run 1 (the calibration run): p50/p95 check 995/1 786 ms, accessible_folders 845/1 579 ms, audit
  query 1 280/2 178 ms; 47 requests/s; seeding 109 s; 3 audit queries answered 503.
* run 2: check 1 063/1 980 ms, accessible_folders 968/1 810 ms, audit query 1 363/2 244 ms;
  43 requests/s; 5 audit queries (all as carol) answered 503.
* Sequential, unloaded service times on the same stack: carol's check 23-28 ms (allow 23, a
  denial with its outbox row 25-28), her accessible_folders 27 ms, the Global admin's check
  11.5 ms, carol's audit query 49 ms, the admin's 26 ms.

At concurrency 50 the single identity worker is saturated (about 45 identity calls per second;
an audit query makes two), so latency is queueing (Little's law: 50 in flight / 45 per second).
The 503s are audit failing closed when its identity call exceeds ``authz_timeout_s`` (2 s). The
zero-5xx gate is kept as specified (the plan forbids weakening it); the capacity gap is recorded
in the Plan 07-09 SUMMARY. ``DC_E2E_LOAD_CONCURRENCY`` (default 25, sized to one identity
worker) changes the concurrency; 50 overloads a single worker (G-07-09-1).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import random
import secrets
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

# Off by default (scripts/e2e.sh): Docker on a laptop cannot hold the profile; DC_PERFORMANCE=1
# runs it on hardware sized for it.
pytestmark = [pytest.mark.e2e, pytest.mark.performance]

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
CHECK = "/internal/v1/authz/check"
ACCESSIBLE = "/internal/v1/authz/accessible-folders"
AUDIT_EVENTS = "/api/v1/audit/events"
DEV_OUT = Path("/dev-out")
RESULT_FILE = DEV_OUT / "phase7-load.json"

ENTRA_DATA_ENG_GROUP = "9a0f0000-0000-4000-8000-0000000000a1"
SEED_CONCURRENCY = 8
# Default of DC_E2E_LOAD_CONCURRENCY. One identity worker saturates near 45 checks/s, so 50
# overloads it (G-07-09-1); 25 is the dev-topology profile.
LOAD_CONCURRENCY = 25
NOISE_USERS = 120
NOISE_GROUPS = 80
TEAM_GRANTS = 5
DELIVERY_WINDOW_S = 180.0
SETTLE_S = 5.0
PAGE_LIMIT = 200

# The first measured p95 per operation (ms) on the calibration machine (module docstring).
MEASURED_P95_MS = {"check": 1786.0, "accessible_folders": 1578.9, "audit_query": 2177.6}
# Research defaults (07-RESEARCH.md, Pattern 10): the floor of every ceiling.
RESEARCH_P95_MS = {"check": 500.0, "accessible_folders": 1000.0, "audit_query": 1500.0}
DEFAULT_P95_MS = {op: max(5 * MEASURED_P95_MS[op], RESEARCH_P95_MS[op]) for op in RESEARCH_P95_MS}
CEILING_ENV = {
    "check": "DC_E2E_LOAD_P95_CHECK_MS",
    "accessible_folders": "DC_E2E_LOAD_P95_FOLDERS_MS",
    "audit_query": "DC_E2E_LOAD_P95_AUDIT_MS",
}

LEVEL_ROLES = {
    "organization": ("org_admin", "org_reader", "consumer", "policy_manager"),
    "project": ("project_admin", "project_editor", "consumer", "policy_manager"),
    "team": ("team_admin", "team_editor", "team_reader", "consumer", "policy_manager"),
}
EXPECTED_CHECK = {"allow": "allow", "not_found": "not_found", "forbidden": "forbidden"}


def _env_number(name: str, default: float) -> float:
    """A number from the environment; unset or empty means ``default``."""
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _filler_group(index: int) -> str:
    """The fake Graph's generated group ``index`` (``infra/fake-graph/app.py``)."""
    return f"00000000-0000-4000-8000-{index:012x}"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, round(fraction * len(ordered)) - 1))
    return ordered[position]


async def _bounded[T](concurrency: int, jobs: list[Callable[[], Awaitable[T]]]) -> list[T]:
    """Run ``jobs`` with at most ``concurrency`` in flight; results in job order."""
    semaphore = asyncio.Semaphore(concurrency)

    async def run(job: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await job()

    return list(await asyncio.gather(*(run(job) for job in jobs)))


@dataclass
class Tree:
    orgs: list[str]
    projects: dict[str, list[str]] = field(default_factory=dict)
    teams: dict[str, list[str]] = field(default_factory=dict)  # org id -> its 9 team ids

    def folders(self) -> list[tuple[str, str]]:
        """Every (folder id, kind) of the run."""
        found = [(org, "organization") for org in self.orgs]
        for org in self.orgs:
            found += [(p, "project") for p in self.projects[org]]
            found += [(t, "team") for t in self.teams[org]]
        return found


class Seeder:
    def __init__(self, identity: httpx.AsyncClient, admin_tokens: Any) -> None:
        self._identity = identity
        self._admin_tokens = admin_tokens

    async def _admin(self) -> dict[str, str]:
        return _bearer(await self._admin_tokens.get())

    async def folder(self, parent: str, slug: str) -> str:
        response = await self._identity.post(
            FOLDERS,
            json={"parent_id": parent, "slug": slug, "display_name": f"Phase 7 load {slug}"},
            headers=await self._admin(),
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    async def user(self, name: str) -> str:
        response = await self._identity.post(
            f"{IDENTITY}/users",
            json={"username": name, "password": secrets.token_urlsafe(24), "display_name": name},
            headers=await self._admin(),
        )
        assert response.status_code == 201, response.text
        return str(response.json()["principal_id"])

    async def local_group(self, owner: str, name: str) -> str:
        response = await self._identity.post(
            f"{IDENTITY}/groups",
            json={"owner_folder_id": owner, "name": name},
            headers=await self._admin(),
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    async def register_entra_group(self, owner: str, object_id: str) -> None:
        response = await self._identity.post(
            f"{IDENTITY}/entra/groups",
            json={"owner_folder_id": owner, "object_id": object_id},
            headers=await self._admin(),
        )
        # 409: an earlier run registered it; the registration is reused.
        assert response.status_code in (200, 201, 409), response.text

    async def grant(self, folder: str, principal_type: str, principal_id: str, role: str) -> None:
        response = await self._identity.post(
            f"{FOLDERS}/{folder}/grants",
            json={"principal_type": principal_type, "principal_id": principal_id, "role": role},
            headers=await self._admin(),
        )
        assert response.status_code == 201, (folder, principal_type, role, response.text)


async def _root_id(identity: httpx.AsyncClient, admin: dict[str, str]) -> str:
    roots = await identity.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root", root
    return str(root["id"])


async def _tree(seeder: Seeder, root: str, run: str, orgs: int) -> Tree:
    org_ids = await _bounded(
        SEED_CONCURRENCY,
        [lambda i=i: seeder.folder(root, f"p7l-{run}-{i:03d}") for i in range(orgs)],
    )
    tree = Tree(orgs=org_ids)
    project_jobs = [
        (org, lambda org=org, p=p: seeder.folder(org, f"proj-{p}"))
        for org in org_ids
        for p in range(3)
    ]
    project_ids = await _bounded(SEED_CONCURRENCY, [job for _, job in project_jobs])
    for (org, _), project in zip(project_jobs, project_ids, strict=True):
        tree.projects.setdefault(org, []).append(project)
    team_jobs = [
        (org, lambda project=project, t=t: seeder.folder(project, f"team-{t}"))
        for org in org_ids
        for project in tree.projects[org]
        for t in range(3)
    ]
    team_ids = await _bounded(SEED_CONCURRENCY, [job for _, job in team_jobs])
    for (org, _), team in zip(team_jobs, team_ids, strict=True):
        tree.teams.setdefault(org, []).append(team)
    return tree


@dataclass(frozen=True)
class Op:
    kind: str  # check | accessible_folders | audit_query
    variant: str  # allow | not_found | forbidden | carol | org_admin
    folder: str | None = None


@dataclass
class Result:
    op: Op
    request_id: str
    status: int | None
    elapsed_ms: float
    outcome: str | None = None
    error: str | None = None
    started: float = 0.0  # time.perf_counter() when the request was sent


def _workload(
    rng: random.Random,
    total: int,
    allow_teams: list[str],
    foreign_teams: list[str],
    forbidden_teams: list[str],
) -> list[Op]:
    checks = round(total * 0.60)
    folders = round(total * 0.25)
    audits = total - checks - folders
    ops: list[Op] = []
    variants = ("allow", "not_found", "forbidden")
    pools = {"allow": allow_teams, "not_found": foreign_teams, "forbidden": forbidden_teams}
    for i in range(checks):
        variant = variants[i % 3]
        ops.append(Op("check", variant, rng.choice(pools[variant])))
    ops += [Op("accessible_folders", "carol") for _ in range(folders)]
    ops += [Op("audit_query", ("org_admin", "carol")[i % 2]) for i in range(audits)]
    rng.shuffle(ops)
    return ops


async def _execute(
    internal: Callable[[str], httpx.AsyncClient],
    op: Op,
    carol: dict[str, str],
    org_admin: dict[str, str],
) -> Result:
    request_id = str(uuid.uuid4())
    rid = {"X-Request-ID": request_id}
    started = time.perf_counter()
    try:
        if op.kind == "check":
            permission = "contract:create" if op.variant == "forbidden" else "contract:read"
            response = await internal("identity").post(
                CHECK,
                json={"permission": permission, "folder_id": op.folder},
                headers={**carol, **rid},
            )
        elif op.kind == "accessible_folders":
            response = await internal("identity").post(
                ACCESSIBLE, json={"permission": "contract:read"}, headers={**carol, **rid}
            )
        else:
            headers = carol if op.variant == "carol" else org_admin
            response = await internal("audit").get(
                AUDIT_EVENTS, params={"limit": 50}, headers={**headers, **rid}
            )
    except httpx.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return Result(op, request_id, None, elapsed, error=type(exc).__name__, started=started)
    elapsed = (time.perf_counter() - started) * 1000
    outcome = None
    if op.kind == "check" and response.status_code == 200:
        outcome = str(response.json()["outcome"])
    return Result(op, request_id, response.status_code, elapsed, outcome=outcome, started=started)


async def _denials(
    audit: httpx.AsyncClient, admin: dict[str, str], since: datetime, wanted: set[str]
) -> list[dict[str, Any]]:
    """Every denial event since ``since`` whose request_id is in ``wanted`` (all pages)."""
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {
            "category": "denial",
            "from": since.isoformat(),
            "limit": PAGE_LIMIT,
        }
        if cursor is not None:
            params["cursor"] = cursor
        response = await audit.get(AUDIT_EVENTS, params=params, headers=admin)
        assert response.status_code == 200, response.text
        body = response.json()
        found.extend(item for item in body["items"] if item["request_id"] in wanted)
        cursor = body["next_cursor"]
        if cursor is None:
            return found


def _stats(times: list[float]) -> dict[str, float]:
    return {
        "count": len(times),
        "p50_ms": round(_percentile(times, 0.50), 1),
        "p95_ms": round(_percentile(times, 0.95), 1),
        "max_ms": round(max(times), 1),
    }


def _summary(results: list[Result]) -> dict[str, dict[str, float]]:
    """p50/p95/max per operation, plus per check outcome and per audit caller."""
    stats = {
        kind: _stats([r.elapsed_ms for r in results if r.op.kind == kind])
        for kind in ("check", "accessible_folders", "audit_query")
    }
    for kind, variant in (
        ("check", "allow"),
        ("check", "not_found"),
        ("check", "forbidden"),
        ("audit_query", "org_admin"),
        ("audit_query", "carol"),
    ):
        stats[f"{kind}:{variant}"] = _stats(
            [r.elapsed_ms for r in results if (r.op.kind, r.op.variant) == (kind, variant)]
        )
    return stats


def _timeline(results: list[Result], bucket_s: float = 5.0) -> list[dict[str, float]]:
    """Requests sent per ``bucket_s`` window: count, p95, max and non-200 answers."""
    first = min(r.started for r in results)
    buckets: dict[int, list[Result]] = {}
    for r in results:
        buckets.setdefault(int((r.started - first) // bucket_s), []).append(r)
    return [
        {
            "from_s": index * bucket_s,
            "count": len(found),
            "p95_ms": round(_percentile([r.elapsed_ms for r in found], 0.95), 1),
            "max_ms": round(max(r.elapsed_ms for r in found), 1),
            "not_200": sum(1 for r in found if r.status != 200),
        }
        for index, found in sorted(buckets.items())
    ]


async def test_authz_load_profile_with_deep_grants_and_a_251_group_principal(
    internal: Callable[[str], httpx.AsyncClient],
    _admin_tokens: Any,
    users: Any,
    entra_token: Callable[[str], Awaitable[str]],
    wait_for: Callable[..., Any],
) -> None:
    orgs = int(_env_number("DC_E2E_LOAD_ORGS", 50))
    grants = int(_env_number("DC_E2E_LOAD_GRANTS", 5000))
    requests = int(_env_number("DC_E2E_LOAD_REQUESTS", 3000))
    concurrency = int(_env_number("DC_E2E_LOAD_CONCURRENCY", LOAD_CONCURRENCY))
    ceilings = {op: _env_number(CEILING_ENV[op], DEFAULT_P95_MS[op]) for op in CEILING_ENV}
    assert orgs >= 2, orgs
    identity = internal("identity")
    seeder = Seeder(identity, _admin_tokens)
    run = uuid.uuid4().hex[:8]
    rng = random.Random(run)  # noqa: S311 - test data shuffling, not cryptography
    graph = httpx.AsyncClient(
        base_url=os.environ["DC_E2E_FAKE_GRAPH_URL"], timeout=httpx.Timeout(10.0)
    )
    try:
        # --- seeding ------------------------------------------------------------------------
        seed_started = time.monotonic()
        root = await _root_id(identity, _bearer(await _admin_tokens.get()))
        tree = await _tree(seeder, root, run, orgs)
        foreign_count = max(1, orgs // 10)
        visible = tree.orgs[: orgs - foreign_count]
        foreign = tree.orgs[orgs - foreign_count :]

        noise_users = await _bounded(
            SEED_CONCURRENCY,
            [lambda i=i: seeder.user(f"p7l-{run}-u{i:03d}") for i in range(NOISE_USERS)],
        )
        noise_groups = await _bounded(
            SEED_CONCURRENCY,
            [
                lambda i=i: seeder.local_group(root, f"p7l-{run}-g{i:03d}")
                for i in range(NOISE_GROUPS)
            ],
        )
        principals = [("user", p) for p in noise_users] + [("local_group", g) for g in noise_groups]
        folders = tree.folders()
        pairs: set[tuple[int, int]] = set()
        target = min(grants, len(folders) * len(principals))
        while len(pairs) < target:
            pairs.add((rng.randrange(len(folders)), rng.randrange(len(principals))))
        noise_jobs = []
        for folder_index, principal_index in sorted(pairs):
            folder_id, kind = folders[folder_index]
            principal_type, principal_id = principals[principal_index]
            role = rng.choice(LEVEL_ROLES[kind])
            noise_jobs.append(
                lambda f=folder_id, t=principal_type, p=principal_id, r=role: seeder.grant(
                    f, t, p, r
                )
            )
        await _bounded(SEED_CONCURRENCY, noise_jobs)

        # carol: Data Eng admits her; her filler groups carry the per-run grants.
        await seeder.register_entra_group(root, ENTRA_DATA_ENG_GROUP)
        team_granted = [tree.teams[org][0] for org in visible[:TEAM_GRANTS]]
        filler = {org: _filler_group(i + 1) for i, org in enumerate(visible)}
        team_filler = {
            team: _filler_group(len(visible) + i + 1) for i, team in enumerate(team_granted)
        }
        for index, group in enumerate([*filler.values(), *team_filler.values()], start=1):
            named = await graph.post(
                f"/_fake/groups/{group}", json={"displayName": f"Load filler {index}"}
            )
            assert named.status_code == 200, named.text
        all_filler = [*filler.values(), *team_filler.values()]
        await _bounded(
            SEED_CONCURRENCY, [lambda g=g: seeder.register_entra_group(root, g) for g in all_filler]
        )
        carol_jobs = [
            lambda o=org, g=group: seeder.grant(o, "entra_group", g, "org_reader")
            for org, group in filler.items()
        ] + [
            lambda t=team, g=group: seeder.grant(t, "entra_group", g, "team_admin")
            for team, group in team_filler.items()
        ]
        await _bounded(SEED_CONCURRENCY, carol_jobs)

        org_admin_user = await users.create("p7loa")
        await seeder.grant(visible[-1], "user", org_admin_user.principal_id, "org_admin")
        seed_s = time.monotonic() - seed_started

        carol = _bearer(await entra_token("carol"))
        org_admin = _bearer(await users.token(org_admin_user))
        allow_teams = [team for org in visible for team in tree.teams[org]]
        foreign_teams = [team for org in foreign for team in tree.teams[org]]
        forbidden_teams = [team for team in allow_teams if team not in set(team_granted)]

        # Warm-up and a correctness probe: carol's 251 groups are resolved once (directory cache).
        async def carol_reads_team() -> bool:
            probe = await identity.post(
                CHECK,
                json={"permission": "contract:read", "folder_id": allow_teams[0]},
                headers=carol,
            )
            return probe.status_code == 200 and probe.json()["outcome"] == "allow"

        await wait_for(carol_reads_team, 60.0, description="carol reads a team through a group")
        scope = await identity.post(ACCESSIBLE, json={"permission": "contract:read"}, headers=carol)
        assert scope.status_code == 200, scope.text
        ids = set(scope.json()["folder_ids"])
        assert scope.json()["all"] is False
        assert set(allow_teams) <= ids
        assert not set(foreign_teams) & ids

        # --- workload -----------------------------------------------------------------------
        since = datetime.now(UTC) - timedelta(seconds=5)
        ops = _workload(rng, requests, allow_teams, foreign_teams, forbidden_teams)
        load_started = time.monotonic()
        results = await _bounded(
            concurrency, [lambda op=op: _execute(internal, op, carol, org_admin) for op in ops]
        )
        load_s = time.monotonic() - load_started
    finally:
        try:
            await graph.post("/_fake/reset")
        finally:
            await graph.aclose()

    stats = _summary(results)
    report = {
        "run": run,
        "measured_at": datetime.now(UTC).isoformat(),
        "machine": {"platform": platform.platform(), "cpus": os.cpu_count()},
        "sizes": {
            "orgs": orgs,
            "folders": len(tree.folders()),
            "noise_principals": len(principals),
            "noise_grants": len(noise_jobs),
            "carol_group_grants": len(carol_jobs),
            "requests": requests,
            "concurrency": concurrency,
        },
        "seed_s": round(seed_s, 1),
        "load_s": round(load_s, 1),
        "throughput_rps": round(requests / load_s, 1),
        "status_counts": dict(Counter(str(r.status) for r in results)),
        "errors": dict(Counter(r.error for r in results if r.error)),
        "operations": stats,
        "timeline": _timeline(results),
        "not_200": [
            {"op": r.op.kind, "variant": r.op.variant, "status": r.status, "error": r.error}
            for r in results
            if r.status != 200
        ],
        "ceilings_p95_ms": ceilings,
    }
    await asyncio.to_thread(RESULT_FILE.write_text, json.dumps(report, indent=2), encoding="utf-8")
    print("phase7-load:", json.dumps(report, indent=2))

    # --- correctness gates ------------------------------------------------------------------
    errors = [r for r in results if r.error is not None]
    assert not errors, f"{len(errors)} transport errors: {report['errors']}"
    server_errors = [r for r in results if r.status is not None and r.status >= 500]
    assert not server_errors, f"{len(server_errors)} 5xx responses: {report['status_counts']}"
    unexpected = [
        (r.op, r.status, r.outcome)
        for r in results
        if r.status != 200 or (r.op.kind == "check" and r.outcome != EXPECTED_CHECK[r.op.variant])
    ]
    assert not unexpected, f"{len(unexpected)} unexpected answers, first: {unexpected[:5]}"

    checks = [r for r in results if r.op.kind == "check"]
    denied = {r.request_id for r in checks if r.outcome != "allow"}
    allowed = {r.request_id for r in checks if r.outcome == "allow"}
    admin = _bearer(await _admin_tokens.get())

    async def all_denials() -> list[dict[str, Any]] | None:
        rows = await _denials(internal("audit"), admin, since, denied | allowed)
        return rows if {row["request_id"] for row in rows} >= denied else None

    await wait_for(
        all_denials,
        DELIVERY_WINDOW_S,
        interval_s=2.0,
        description=f"{len(denied)} denial events by request id",
    )
    await asyncio.sleep(SETTLE_S)
    rows = await _denials(internal("audit"), admin, since, denied | allowed)
    counts = Counter(row["request_id"] for row in rows)
    assert not {rid for rid, n in counts.items() if n > 1}, "a denial was recorded twice"
    assert set(counts) == denied, "allowed checks must not be recorded, denied ones must"

    # --- calibrated ceilings ----------------------------------------------------------------
    over = {
        op: (stats[op]["p95_ms"], ceilings[op])
        for op in ceilings
        if stats[op]["p95_ms"] > ceilings[op]
    }
    assert not over, f"p95 above its ceiling (measured, ceiling): {over}"
