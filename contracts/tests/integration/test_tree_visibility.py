"""TREE-04 (D-17): the Folders tree query shows each persona exactly what the oracle allows.

The tree asks the contract list once per expanded folder (``folder_id=F``,
``include_subtree=false``, ``sort=name``). For every persona of D-17 the union of those
per-folder answers must equal three independent answers: the ``dc_auth.oracle`` set computed
from the same grants and block flags, the plain contract list (``include_subtree=true`` at
ROOT) and a hand-written literal. No contract name or ODCS id outside that set may appear in
any response body, a contract is returned by exactly one folder query (its own), and a
persona without a grant gets 200 with an empty page, never a 403.

The reachability rule is scoped to the shipped 2,000-folder cap (user decision 2026-11-27):
the tree can reach every visible contract whose folder is in the persona's folder list, which
holds for every folder of this world, far below the cap. Beyond the cap a contract is reached
by opening a folder, which this test does not exercise.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from dc_auth.oracle import Grant, Tree, TreeFolder, folders_with

pytestmark = pytest.mark.integration

LIST = "/api/v1/contracts/contracts"
FOLDERS = "/api/v1/identity/folders"
StackFactory = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, name: str, version: str) -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: draft\nname: {name}\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def block(stack: Any, folder_id: str) -> None:
    url = stack.identity_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("UPDATE folder SET block_inheritance = true WHERE id = %s", (folder_id,))


@dataclass(frozen=True)
class Seeded:
    key: str
    id: str
    folder: str
    name: str
    odcs_id: str
    latest: bool


@dataclass
class World:
    run: str
    root: str
    org_a: str
    project_a: str
    ta1: str
    project_2: str
    ta2: str
    project_b: str
    tab: str
    org_b: str
    project_tb: str
    tb: str
    parents: dict[str, str | None]
    kinds: dict[str, str]
    contracts: dict[str, Seeded] = field(default_factory=dict)

    @property
    def folders(self) -> list[str]:
        return list(self.parents)


async def publish_contract(
    stack: Any,
    folder: str,
    key: str,
    run: str,
    *,
    publish: bool = True,
    deprecate_latest: bool = False,
    unpublish_preview: bool = False,
) -> Seeded:
    """One contract with a unique ASCII name and ODCS id; the flags set its lifecycle shape."""
    name = f"Tree {key} {run}"
    odcs_id = f"urn:dc:tree:{run}:{key}"
    contract, draft, etag = await stack.create_contract(folder, doc(odcs_id, name, "1.0.0"))
    if publish:
        first = await stack.publish(contract, draft, etag)
        assert first.status_code == 201, first.text
    base = f"/api/v1/contracts/contracts/{contract}"
    if unpublish_preview:
        created = await stack.client.post(
            f"{base}/drafts",
            json={"from_version": "1.0.0", "bump": "none"},
            headers=bearer(stack.admin_token),
        )
        assert created.status_code == 201, created.text
        new_draft = created.json()["draft"]
        saved = await stack.client.put(
            f"{base}/drafts/{new_draft['id']}",
            content=doc(odcs_id, name, "1.1.0"),
            headers={
                **bearer(stack.admin_token),
                "Content-Type": "application/yaml",
                "If-Match": new_draft["etag"],
            },
        )
        assert saved.status_code == 200, saved.text
        published = await stack.publish(contract, new_draft["id"], saved.headers["etag"])
        assert published.status_code == 201, published.text
        gone = await stack.client.post(
            f"{base}/versions/1.1.0/unpublish", headers=bearer(stack.admin_token)
        )
        assert gone.status_code == 200, gone.text
    if deprecate_latest:
        sunset = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
        flagged = await stack.client.post(
            f"{base}/versions/1.0.0/deprecate",
            json={"sunset_date": sunset},
            headers=bearer(stack.admin_token),
        )
        assert flagged.status_code == 200, flagged.text
    return Seeded(key, contract, folder, name, odcs_id, latest=publish)


async def build_world(stack: Any) -> World:
    """ROOT, org A (project PA with teams TA1, TA2 and the blocked TAB), org B with team TB."""
    run = uuid.uuid4().hex[:8]
    a = await stack.chain(f"tva{run}")
    project_2 = await stack.folder(a.org, "p-two")
    ta2 = await stack.folder(project_2, "t-two")
    project_b = await stack.folder(a.org, "p-blk")
    tab = await stack.folder(project_b, "t-blk")
    b = await stack.chain(f"tvb{run}")
    block(stack, tab)
    root = stack.root_id
    parents: dict[str, str | None] = {
        root: None,
        a.org: root,
        a.project: a.org,
        a.team: a.project,
        project_2: a.org,
        ta2: project_2,
        project_b: a.org,
        tab: project_b,
        b.org: root,
        b.project: b.org,
        b.team: b.project,
    }
    kinds = {
        root: "root",
        a.org: "organization",
        a.project: "project",
        a.team: "team",
        project_2: "project",
        ta2: "team",
        project_b: "project",
        tab: "team",
        b.org: "organization",
        b.project: "project",
        b.team: "team",
    }
    world = World(
        run=run,
        root=root,
        org_a=a.org,
        project_a=a.project,
        ta1=a.team,
        project_2=project_2,
        ta2=ta2,
        project_b=project_b,
        tab=tab,
        org_b=b.org,
        project_tb=b.project,
        tb=b.team,
        parents=parents,
        kinds=kinds,
    )
    specs: list[tuple[str, str, dict[str, bool]]] = [
        ("c01-ta1-draft", a.team, {"publish": False}),
        ("c02-ta1-latest", a.team, {}),
        ("c03-ta1-deprecated", a.team, {"deprecate_latest": True}),
        ("c04-ta1-unpublished", a.team, {"unpublish_preview": True}),
        ("c05-ta2-latest", ta2, {}),
        ("c06-tab-latest", tab, {}),
        ("c07-tb-latest", b.team, {}),
        ("c08-tb-draft", b.team, {"publish": False}),
        ("c09-orga-latest", a.org, {}),
        ("c10-root-latest", root, {}),
    ]
    for key, folder, flags in specs:
        world.contracts[key] = await publish_contract(stack, folder, key, run, **flags)
    return world


@dataclass(frozen=True)
class Persona:
    sub: str
    grants: tuple[tuple[str, str], ...]  # (role, World attribute naming the folder)
    # Hand-written, independent of the oracle: the contract keys the persona may see.
    sees: frozenset[str]
    redacted: bool = False


ALL = frozenset(f"c{n:02d}" for n in range(1, 11))


def keys(*numbers: int) -> frozenset[str]:
    return frozenset(f"c{n:02d}" for n in numbers)


# TAB is a blocked-inheritance team: grants above it do not reach it (D-29), except the Global admin
# (D-28); a grant made at TAB itself still applies there. A consumer reads published contracts only.
PERSONAS: dict[str, Persona] = {
    "global_admin": Persona("gad", (("global_admin", "root"),), ALL),
    "org_admin": Persona("oad", (("org_admin", "org_a"),), keys(1, 2, 3, 4, 5, 9)),
    "team_editor": Persona("ted", (("team_editor", "ta1"),), keys(1, 2, 3, 4)),
    "consumer": Persona("con", (("consumer", "ta1"),), keys(2, 3, 4)),
    "policy_manager": Persona(
        "pmg", (("policy_manager", "org_a"),), keys(1, 2, 3, 4, 5, 9), redacted=True
    ),
    "folder_only_grant": Persona("fog", (("team_reader", "ta2"),), keys(5)),
    "org_reader_blocked_team": Persona("orb", (("org_reader", "org_a"),), keys(1, 2, 3, 4, 5, 9)),
    "grant_on_blocked_team": Persona("gbt", (("team_reader", "tab"),), keys(6)),
    "no_grant": Persona("nog", (), frozenset()),
}


def oracle_ids(world: World, persona: Persona, principal_id: str) -> set[str]:
    """The contracts the access oracle allows: metadata anywhere, or published with a Latest."""
    tree = Tree(
        {
            UUID(fid): TreeFolder(UUID(fid), UUID(parent) if parent else None, world.kinds[fid])
            for fid, parent in world.parents.items()
        }
    )
    blocks = {UUID(world.tab)}
    grants = [
        Grant(UUID(getattr(world, attr)), "user", principal_id, role)
        for role, attr in persona.grants
    ]
    grantees = {("user", principal_id)}
    metadata = {
        str(f) for f in folders_with(tree, blocks, grants, grantees, "contract:read-metadata")
    }
    published = {
        str(f) for f in folders_with(tree, blocks, grants, grantees, "contract:read-published")
    }
    return {
        c.id
        for c in world.contracts.values()
        if c.folder in metadata or (c.folder in published and c.latest)
    }


async def list_pages(stack: Any, token: str, **params: Any) -> list[tuple[dict[str, Any], str]]:
    """Every row of a list query, following ``next_cursor``; each row with its body's text."""
    rows: list[tuple[dict[str, Any], str]] = []
    cursor: str | None = None
    while True:
        query = {**params, **({"cursor": cursor} if cursor else {})}
        response = await stack.client.get(LIST, params=query, headers=bearer(token))
        assert response.status_code == 200, response.text
        body = response.json()
        rows.extend((item, response.text) for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return rows


async def tree_query(
    stack: Any, token: str, folder: str, *, limit: int = 100
) -> list[tuple[dict[str, Any], str]]:
    """What the tree sends for one expanded folder."""
    return await list_pages(
        stack, token, folder_id=folder, include_subtree="false", sort="name", limit=limit
    )


async def tree_ids(
    stack: Any, token: str, world: World
) -> tuple[dict[str, list[str]], list[dict[str, Any]], list[str]]:
    """Union over every world folder of the tree query.

    Returns contract id -> the folders whose query returned it, every row, and every body text.
    """
    seen_in: dict[str, list[str]] = {}
    rows: list[dict[str, Any]] = []
    bodies: list[str] = []
    for folder in world.folders:
        for item, text in await tree_query(stack, token, folder):
            seen_in.setdefault(item["id"], []).append(folder)
            rows.append(item)
            bodies.append(text)
        # An empty folder still answers a body: scan it too.
        response = await stack.client.get(
            LIST,
            params={"folder_id": folder, "include_subtree": "false", "sort": "name", "limit": 100},
            headers=bearer(token),
        )
        bodies.append(response.text)
    return seen_in, rows, bodies


async def folder_ids(stack: Any, token: str) -> set[str]:
    found: set[str] = set()
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100, **({"cursor": cursor} if cursor else {})}
        response = await stack.identity.get(FOLDERS, params=params, headers=bearer(token))
        assert response.status_code == 200, response.text
        body = response.json()
        found.update(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return found


@pytest.mark.parametrize("persona_key", list(PERSONAS))
async def test_the_tree_shows_each_persona_exactly_the_oracle_set(
    make_stack: StackFactory, persona_key: str
) -> None:
    persona = PERSONAS[persona_key]
    async with make_stack() as stack:
        world = await build_world(stack)
        token, principal_id = stack.token(persona.sub)
        for role, attr in persona.grants:
            stack.grant_sql(principal_id, role, getattr(world, attr))
        seeded = {c.id: c for c in world.contracts.values()}

        expected = {c.id for c in world.contracts.values() if c.key[:3] in persona.sees}
        oracle = oracle_ids(world, persona, principal_id)
        assert oracle == expected  # the literal and the oracle agree, so neither is vacuous

        seen_in, rows, bodies = await tree_ids(stack, token, world)
        tree_set = set(seen_in) & set(seeded)
        assert tree_set == oracle

        plain_rows = await list_pages(
            stack, token, folder_id=world.root, include_subtree="true", limit=100
        )
        plain_set = {item["id"] for item, _ in plain_rows} & set(seeded)
        assert plain_set == oracle

        # No name or ODCS id of a contract outside the oracle set appears in any body.
        everything = "\n".join(bodies + [text for _, text in plain_rows])
        for contract in world.contracts.values():
            if contract.id in oracle:
                continue
            assert contract.name not in everything, contract.key
            assert contract.odcs_id not in everything, contract.key
            assert contract.id not in everything, contract.key

        # A contract is returned by exactly one folder query: its own, never an ancestor's.
        for contract_id in oracle:
            assert seen_in[contract_id] == [seeded[contract_id].folder], seeded[contract_id].key
        for item in rows:
            assert item["folder_id"] in world.parents

        # Reachability within the 2,000-folder cap: every folder holding a visible contract is
        # in the persona's folder list, so the tree can expand down to it.
        listed_folders = await folder_ids(stack, token)
        for contract_id in oracle:
            assert seeded[contract_id].folder in listed_folders, seeded[contract_id].key

        # content_redacted is true on exactly the Policy Manager's rows.
        for item in rows:
            if item["id"] in seeded:
                assert item["content_redacted"] is persona.redacted, item["name"]

        # Each folder's rows are in name order (the tree's order) with an exhausted cursor.
        for folder in world.folders:
            names = [
                item["name"] or item["odcs_id"]
                for item, _ in await tree_query(stack, token, folder)
            ]
            assert names == sorted(names, key=str.lower)

        if not persona.sees:
            for folder in world.folders:
                response = await stack.client.get(
                    LIST,
                    params={
                        "folder_id": folder,
                        "include_subtree": "false",
                        "sort": "name",
                        "limit": 100,
                    },
                    headers=bearer(token),
                )
                assert response.status_code == 200, response.text
                assert response.json() == {"items": [], "next_cursor": None}


async def test_a_folder_only_grant_lists_its_team_while_the_parents_stay_unseen_and_pages_agree(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        world = await build_world(stack)
        token, principal_id = stack.token("collibra")
        stack.grant_sql(principal_id, "team_reader", world.ta2)
        # The persona holds nothing on org A or project 2: those folders list no contract and are
        # not in its folder list, yet the team's own contracts are returned (never filtered by
        # "the parent is also visible").
        listed = await folder_ids(stack, token)
        assert world.ta2 in listed
        assert world.org_a not in listed and world.project_2 not in listed
        for parent in (world.org_a, world.project_2):
            assert await tree_query(stack, token, parent) == []
        team_rows = await tree_query(stack, token, world.ta2)
        assert [item["id"] for item, _ in team_rows] == [world.contracts["c05-ta2-latest"].id]

        # Name pages are the same rows as one big page, whatever the page size.
        admin = stack.admin_token
        for folder in (world.ta1, world.ta2, world.root):
            whole = [item["id"] for item, _ in await tree_query(stack, admin, folder)]
            paged = [item["id"] for item, _ in await tree_query(stack, admin, folder, limit=1)]
            assert paged == whole
        assert len(await tree_query(stack, admin, world.ta1)) == 4
