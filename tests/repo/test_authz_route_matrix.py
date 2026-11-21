"""Generated role x operation authorization matrix for every public API operation.

For each operation of every routed service (the live route tables, built in-process exactly like
the OpenAPI export) this test takes the route's own ``require()`` policy dependency, mounts it in
front of a stub endpoint, and calls it as every role of ``dc_auth.roles`` in several placements.
The Authorizer behind the policy is ``dc_auth.oracle`` (the literal reading of the access rules;
identity's integration suite proves the SQL decision point equals it, ``test_authz_oracle.py``),
so what is checked here is the HTTP layer of each operation: the permission it declares, whether
it accepts a restricted bootstrap session, which folder kind it is evaluated at, and the status a
refusal maps to (404 when the folder is not visible, existence hiding; 403 when it is).

Folder tree (one Global ROOT):

    ROOT -> org_a -> proj_a -> team_a
         -> org_b -> proj_b -> team_b

Each role is granted once, at the highest folder kind it binds to on the ``a`` branch (ROOT for the
Global roles). Every operation is then called with the route's target folder set to ``org_a``,
``team_a`` and ``team_b`` (outside the grant). Two more columns: a principal with no grant at all
and a restricted bootstrap session (``password:rotate`` only).

The expected outcome of every cell is pinned in ``authz_matrix.golden.json`` next to this module,
so a change to a route's permission, to the role map or to the refusal mapping is a reviewed diff:

    uv run python tests/repo/test_authz_route_matrix.py --update   rewrite the golden file

A cell is ``200`` (the policy allowed and the stub answered), ``403`` or ``404`` (the policy
refused), or ``401`` (no principal). ``special`` permissions (``password:rotate``,
``authz:check``) have dedicated branches in identity's decision point and are tested there; public
operations are listed with ``"public"`` and called once without a token.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from dc_auth import oracle
from dc_auth.authorizer import Decision, FolderFacts, FolderScope, Outcome
from dc_auth.deps import authenticate
from dc_auth.principal import Principal
from dc_auth.roles import PERMISSIONS, ROLE_LEVELS, Role
from dc_core.policy import POLICY_ATTR, PolicyMeta
from dc_core.problems import install_problem_handlers
from fastapi import Depends, FastAPI, Response
from fastapi.routing import APIRoute, iter_route_contexts

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).with_name("authz_matrix.golden.json")
ROUTED = ("identity", "contracts", "validation", "audit")
KIND_ORDER = ("root", "organization", "project", "team")


def _uid(name: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"dc-authz-matrix/{name}")


ROOT, ORG_A, PROJ_A, TEAM_A = _uid("root"), _uid("org_a"), _uid("proj_a"), _uid("team_a")
ORG_B, PROJ_B, TEAM_B = _uid("org_b"), _uid("proj_b"), _uid("team_b")
_FOLDERS = (
    oracle.TreeFolder(ROOT, None, "root"),
    oracle.TreeFolder(ORG_A, ROOT, "organization"),
    oracle.TreeFolder(PROJ_A, ORG_A, "project"),
    oracle.TreeFolder(TEAM_A, PROJ_A, "team"),
    oracle.TreeFolder(ORG_B, ROOT, "organization"),
    oracle.TreeFolder(PROJ_B, ORG_B, "project"),
    oracle.TreeFolder(TEAM_B, PROJ_B, "team"),
)
TREE = oracle.Tree({f.id: f for f in _FOLDERS})
A_CHAIN = {"root": ROOT, "organization": ORG_A, "project": PROJ_A, "team": TEAM_A}
TARGETS = (("org_a", ORG_A), ("team_a", TEAM_A), ("team_b", TEAM_B))
USER_ID = "local:matrix-user"


def _facts(folder_id: UUID) -> FolderFacts:
    folder = TREE.folders[folder_id]
    chain = TREE.chain(folder_id)
    org = next((f for f in chain if TREE.folders[f].kind == "organization"), None)
    return FolderFacts(
        id=folder_id,
        kind=folder.kind,  # type: ignore[arg-type]
        slug=str(folder_id)[:8],
        path=".".join(str(f).replace("-", "") for f in chain),
        ancestor_ids=chain,
        organization_id=org,
        archived=False,
    )


class OracleAuthorizer:
    """An ``Authorizer`` whose answers are ``dc_auth.oracle.decide`` over ``TREE``."""

    def __init__(self) -> None:
        self.grants: list[oracle.Grant] = []

    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: UUID | None = None,
        *,
        fresh: bool = False,
    ) -> Decision:
        grantees = {("user", principal.id)}
        outcome = Outcome(oracle.decide(TREE, (), self.grants, grantees, permission, folder_id))
        if outcome is not Outcome.ALLOW:
            return Decision(outcome=outcome)
        facts = _facts(folder_id) if folder_id in TREE.folders else None
        evaluated = folder_id if folder_id in TREE.folders else ROOT
        held = oracle.permissions_at(TREE, (), self.grants, grantees, evaluated)
        return Decision(outcome=outcome, folder=facts, permissions=held)

    async def accessible_folders(self, principal: Principal, permission: str) -> FolderScope:
        raise AssertionError("a route policy never asks for a scope")


def _principal(*, restricted: bool = False) -> Principal:
    return Principal(
        id=USER_ID,
        type="user",
        display="Matrix User",
        issuer="https://matrix.test",
        subject="matrix-user",
        via="jwt",
        scopes=frozenset({"password:rotate"} if restricted else {"api"}),
    )


def _policy_call(dependant: Any) -> Callable[..., Any] | None:
    for dep in dependant.dependencies:
        if isinstance(getattr(dep.call, POLICY_ATTR, None), PolicyMeta):
            return dep.call  # type: ignore[no-any-return]
        nested = _policy_call(dep)
        if nested is not None:
            return nested
    return None


def _target_dependency(policy: Callable[..., Any], route: APIRoute) -> Callable[..., Any] | None:
    """The ``target=`` dependency of a ``require()`` policy (None for ``_no_target``)."""
    for dep in route.dependant.dependencies:
        found = _find_dependant(dep, policy)
        if found is not None:
            for sub in found.dependencies:
                if sub.call is authenticate:
                    continue
                name = getattr(sub.call, "__name__", "")
                return None if name == "_no_target" else sub.call  # type: ignore[no-any-return]
    return None


def _find_dependant(dependant: Any, call: Callable[..., Any]) -> Any:
    if dependant.call is call:
        return dependant
    for dep in dependant.dependencies:
        found = _find_dependant(dep, call)
        if found is not None:
            return found
    return None


def _operations(apps: dict[str, FastAPI]) -> Iterator[tuple[str, str, str, APIRoute]]:
    """``(operationId, METHOD, path, route)`` for every operation in the routed documents."""
    for service in ROUTED:
        app = apps[service]
        document = app.openapi()
        by_key: dict[tuple[str, str], APIRoute] = {}
        for rc in iter_route_contexts(app.routes):
            if isinstance(rc.original_route, APIRoute):
                for method in rc.methods or ():
                    by_key[(method, rc.path or "")] = rc.original_route
        for path, item in document.get("paths", {}).items():
            for method, operation in item.items():
                if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    continue
                route = by_key.get((method.upper(), path))
                assert route is not None, f"{method.upper()} {path} is documented but not routed"
                yield operation["operationId"], method.upper(), path, route


def _grant_folder(role: Role) -> UUID:
    kind = min(ROLE_LEVELS[role], key=KIND_ORDER.index)
    return A_CHAIN[kind]


async def _cell(
    client: httpx.AsyncClient, app: FastAPI, method: str, url: str, principal: Principal | None
) -> str:
    if principal is None:
        app.dependency_overrides.pop(authenticate, None)
    else:
        app.dependency_overrides[authenticate] = lambda: principal
    response = await client.request(method, url)
    return str(response.status_code)


async def _row_for(method: str, path: str, route: APIRoute) -> dict[str, Any]:
    policy = _policy_call(route.dependant)
    assert policy is not None, f"{method} {path} has no policy dependency"
    meta: PolicyMeta = getattr(policy, POLICY_ATTR)
    url = path
    while "{" in url:
        start, end = url.index("{"), url.index("}")
        url = url[:start] + str(uuid.uuid4()) + url[end + 1 :]

    app = FastAPI()
    install_problem_handlers(app)
    authz = OracleAuthorizer()
    app.state.authorizer_factory = lambda request: authz

    async def stub() -> Response:
        return Response(status_code=200)

    app.add_api_route(path, stub, methods=[method], dependencies=[Depends(policy)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://matrix") as client:
        if meta.public:
            return {
                "permission": None,
                "target": "public",
                "anonymous": await _cell(client, app, method, url, None),
            }
        spec = PERMISSIONS[meta.permission or ""]
        row: dict[str, Any] = {
            "permission": meta.permission,
            "target": spec.target,
            "allow_restricted": meta.allow_restricted,
            "anonymous": await _cell(client, app, method, url, None),
        }
        if spec.target == "special":
            return row
        target_dep = _target_dependency(policy, route)
        targets = TARGETS if target_dep is not None else (("none", None),)
        roles: dict[str, str] = {}
        for role in Role:
            authz.grants = [oracle.Grant(_grant_folder(role), "user", USER_ID, str(role))]
            cells = []
            for _label, folder in targets:
                if target_dep is not None:
                    app.dependency_overrides[target_dep] = lambda folder=folder: folder
                cells.append(await _cell(client, app, method, url, _principal()))
            roles[str(role)] = "/".join(cells)
        authz.grants = []
        no_grant = []
        restricted = []
        for _label, folder in targets:
            if target_dep is not None:
                app.dependency_overrides[target_dep] = lambda folder=folder: folder
            no_grant.append(await _cell(client, app, method, url, _principal()))
            authz.grants = [oracle.Grant(ROOT, "user", USER_ID, str(Role.GLOBAL_ADMIN))]
            restricted.append(await _cell(client, app, method, url, _principal(restricted=True)))
            authz.grants = []
        row["targets"] = "/".join(label for label, _ in targets)
        row["roles"] = roles
        row["no_grant"] = "/".join(no_grant)
        row["restricted_global_admin"] = "/".join(restricted)
        return row


async def build_matrix(apps: dict[str, FastAPI]) -> dict[str, Any]:
    matrix: dict[str, Any] = {}
    for operation_id, method, path, route in _operations(apps):
        row = await _row_for(method, path, route)
        matrix[operation_id] = {"method": method, "path": path, **row}
    return dict(sorted(matrix.items()))


def render(matrix: dict[str, Any]) -> str:
    return json.dumps(matrix, indent=1, sort_keys=True) + "\n"


# --- tests ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def matrix(service_apps: dict[str, FastAPI]) -> dict[str, Any]:
    return asyncio.run(build_matrix(service_apps))


def test_every_documented_operation_is_in_the_matrix(
    matrix: dict[str, Any], service_apps: dict[str, FastAPI]
) -> None:
    documented = {
        op["operationId"]
        for service in ROUTED
        for item in service_apps[service].openapi().get("paths", {}).values()
        for method, op in item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert documented == set(matrix)
    assert len(matrix) >= 100  # a vacuous walk is a failure


def test_the_matrix_matches_the_reviewed_golden_file(matrix: dict[str, Any]) -> None:
    assert GOLDEN.exists(), "run: uv run python tests/repo/test_authz_route_matrix.py --update"
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    changed = sorted(op for op in set(golden) | set(matrix) if golden.get(op) != matrix.get(op))
    assert not changed, (
        f"authorization matrix changed for {changed[:20]} ({len(changed)} operations); review "
        "the diff and run `uv run python tests/repo/test_authz_route_matrix.py --update`"
    )


def test_every_protected_operation_refuses_an_anonymous_caller(matrix: dict[str, Any]) -> None:
    open_ops = [
        op for op, row in matrix.items() if row["target"] != "public" and row["anonymous"] != "401"
    ]
    assert open_ops == []


def test_refusals_hide_existence_outside_the_grant(matrix: dict[str, Any]) -> None:
    """A folder-scoped operation outside every grant is 404, never 403 (D-08, existence hiding):
    the ``team_b`` cell of every role granted below ROOT, and every cell of the no-grant
    principal."""
    leaks: list[str] = []
    for op, row in matrix.items():
        if row["target"] != "folder" or row.get("targets") != "org_a/team_a/team_b":
            continue
        if set(row["no_grant"].split("/")) != {"404"}:
            leaks.append(f"{op} no_grant={row['no_grant']}")
        for role, cells in row["roles"].items():
            if _grant_folder(Role(role)) == ROOT:
                continue  # granted at ROOT, so team_b is inside the grant
            team_b = cells.split("/")[2]
            if team_b != "404":
                leaks.append(f"{op} {role} team_b={team_b}")
    assert leaks == []


def test_a_restricted_session_reaches_only_operations_that_allow_it(
    matrix: dict[str, Any],
) -> None:
    for op, row in matrix.items():
        if row["target"] in {"public", "special"}:
            continue
        cells = set(row["restricted_global_admin"].split("/"))
        if row["allow_restricted"]:
            assert "403" not in cells or row["target"] == "folder", op
        else:
            assert cells == {"403"}, (op, cells)


def test_the_global_admin_is_allowed_everywhere_except_explicit_carve_outs(
    matrix: dict[str, Any],
) -> None:
    refused = sorted(
        op
        for op, row in matrix.items()
        if row["target"] not in {"public", "special"}
        and set(row["roles"]["global_admin"].split("/")) != {"200"}
    )
    # mail:send is the notifier's alone (D-105); nothing else is withheld from a Global admin.
    assert all(matrix[op]["permission"] == "mail:send" for op in refused), refused


if __name__ == "__main__":
    if sys.argv[1:] != ["--update"]:
        print(__doc__)
        raise SystemExit(2)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import export_openapi

    with tempfile.TemporaryDirectory(prefix="dc-authz-matrix-") as tmp:
        built = export_openapi.build_service_apps(Path(tmp))
        GOLDEN.write_text(render(asyncio.run(build_matrix(built))), encoding="utf-8")
    print(f"wrote {GOLDEN}")
