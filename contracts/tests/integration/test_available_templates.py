"""Templates are visible and usable below their folder (TMPL-02, D-37, T-02-48).

Availability is computed from the target folder's ancestors, never from the caller's grant chain:
a template of ROOT, the Organization or the Project is listed and previewable for an editor in
the Team below, whatever block-inheritance flags say, and never a template of another
organization or a sibling subtree.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"
TEMPLATES = "/api/v1/contracts/templates"
UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def v32_yaml() -> bytes:
    return (FIXTURES / "valid-v3.2.0.yaml").read_bytes()


def changed() -> bytes:
    return v32_yaml().replace(b"Payments Orders", b"Payments Orders v2")


async def make(stack: Any, folder_id: str, name: str, *, publish: bool = True) -> str:
    created = await stack.client.post(
        f"/api/v1/contracts/folders/{folder_id}/templates",
        params={"name": name},
        content=v32_yaml(),
        headers={**bearer(stack.admin_token), "Content-Type": "application/yaml"},
    )
    assert created.status_code == 201, created.text
    template_id = str(created.json()["template"]["id"])
    if publish:
        await publish_draft(stack, template_id)
    return template_id


async def publish_draft(stack: Any, template_id: str) -> None:
    published = await stack.client.post(
        f"{TEMPLATES}/{template_id}/publish", headers=bearer(stack.admin_token)
    )
    assert published.status_code == 201, published.text


async def publish_second_version(stack: Any, template_id: str) -> None:
    updated = await stack.client.put(
        f"{TEMPLATES}/{template_id}/draft",
        content=changed(),
        headers={**bearer(stack.admin_token), "Content-Type": "application/yaml"},
    )
    assert updated.status_code == 200, updated.text
    await publish_draft(stack, template_id)


async def deprecate(stack: Any, template_id: str, version_no: int) -> None:
    response = await stack.client.post(
        f"{TEMPLATES}/{template_id}/versions/{version_no}/deprecate",
        headers=bearer(stack.admin_token),
    )
    assert response.status_code == 200, response.text


async def available(stack: Any, folder_id: str, token: str, **params: Any) -> Any:
    return await stack.client.get(
        f"/api/v1/contracts/folders/{folder_id}/available-templates",
        params=params,
        headers=bearer(token),
    )


async def preview(
    stack: Any, folder_id: str, template_id: str, version_no: int | str, token: str
) -> Any:
    return await stack.client.get(
        f"/api/v1/contracts/folders/{folder_id}/available-templates/{template_id}"
        f"/versions/{version_no}/document",
        headers=bearer(token),
    )


def ids(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["template_id"] for item in response.json()["items"]]


def block(stack: Any, folder_id: str) -> None:
    url = stack.identity_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("UPDATE folder SET block_inheritance = true WHERE id = %s", (folder_id,))


def stable(response: Any) -> dict[str, Any]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return body


async def test_an_editor_lists_the_templates_of_the_folder_and_its_ancestors_only(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain("orga")
        other = await stack.chain("orgb")
        root_t = await make(stack, stack.root_id, "root")
        org_t = await make(stack, chain.org, "org")
        project_t = await make(stack, chain.project, "project")
        sibling_team = await stack.team_in_org(chain.org, "sibling")
        await make(stack, sibling_team, "sibling-team")
        await make(stack, other.org, "other-org")
        await make(stack, other.team, "other-team")
        await make(stack, chain.team, "unpublished", publish=False)
        await publish_second_version(stack, project_t)
        ed, ed_principal = stack.token("ed")
        stack.grant_sql(ed_principal, "team_editor", chain.team)

        response = await available(stack, chain.team, ed)
        assert ids(response) == sorted([root_t, org_t, project_t])
        items = {item["template_id"]: item for item in response.json()["items"]}
        assert response.json()["next_cursor"] is None
        assert items[root_t]["name"] == "root"
        assert items[root_t]["folder_id"] == stack.root_id
        assert items[org_t]["folder_id"] == chain.org
        assert items[project_t]["folder_id"] == chain.project
        assert items[org_t]["latest_version_no"] == 1
        assert items[org_t]["version_nos"] == [1]
        assert items[project_t]["latest_version_no"] == 2
        assert items[project_t]["version_nos"] == [1, 2]


async def test_deprecation_hides_versions_and_a_template_without_a_usable_version(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        only = await make(stack, chain.org, "only")
        two = await make(stack, chain.project, "two")
        await publish_second_version(stack, two)
        ed, ed_principal = stack.token("ed")
        stack.grant_sql(ed_principal, "team_editor", chain.team)
        assert ids(await available(stack, chain.team, ed)) == sorted([only, two])

        await deprecate(stack, only, 1)
        await deprecate(stack, two, 1)
        response = await available(stack, chain.team, ed)
        assert ids(response) == [two]
        item = response.json()["items"][0]
        assert item["version_nos"] == [2]
        assert item["latest_version_no"] == 2

        await deprecate(stack, two, 2)
        assert ids(await available(stack, chain.team, ed)) == []


async def test_block_inheritance_does_not_hide_ancestor_templates_from_a_team_editor(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        root_t = await make(stack, stack.root_id, "root")
        org_t = await make(stack, chain.org, "org")
        project_t = await make(stack, chain.project, "project")
        ed, ed_principal = stack.token("ed")
        stack.grant_sql(ed_principal, "team_editor", chain.team)
        block(stack, chain.org)
        block(stack, chain.team)

        response = await available(stack, chain.team, ed)
        assert ids(response) == sorted([root_t, org_t, project_t])
        for template_id in (root_t, org_t, project_t):
            shown = await preview(stack, chain.team, template_id, 1, ed)
            assert shown.status_code == 200, shown.text


async def test_preview_returns_the_version_bytes_and_hides_deprecated_and_foreign_versions(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        org_t = await make(stack, chain.org, "org")
        foreign = await make(stack, other.org, "foreign")
        await publish_second_version(stack, org_t)
        ed, ed_principal = stack.token("ed")
        stack.grant_sql(ed_principal, "team_editor", chain.team)

        first = await preview(stack, chain.team, org_t, 1, ed)
        assert first.status_code == 200
        assert first.content == v32_yaml()
        assert first.headers["content-type"] == "application/yaml"
        assert first.headers["x-content-type-options"] == "nosniff"
        assert first.headers["vary"] == "Accept"
        assert first.headers["content-disposition"] == "inline"
        converted = await stack.client.get(
            f"/api/v1/contracts/folders/{chain.team}/available-templates/{org_t}"
            "/versions/2/document",
            headers={**bearer(ed), "Accept": "application/json"},
        )
        assert converted.status_code == 200
        assert converted.headers["content-type"] == "application/json"
        assert converted.json()["name"] == "Payments Orders v2"

        await deprecate(stack, org_t, 1)
        gone = await preview(stack, chain.team, org_t, 1, ed)
        unknown_version = await preview(stack, chain.team, org_t, 9, ed)
        foreign_version = await preview(stack, chain.team, foreign, 1, ed)
        unknown_template = await preview(stack, chain.team, UNKNOWN, 1, ed)
        for denied in (gone, unknown_version, foreign_version, unknown_template):
            assert denied.status_code == 404
            assert stable(denied) == stable(unknown_template)


async def test_a_project_editor_lists_the_templates_available_at_the_project(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        root_t = await make(stack, stack.root_id, "root")
        org_t = await make(stack, chain.org, "org")
        project_t = await make(stack, chain.project, "project")
        team_t = await make(stack, chain.team, "team")
        await make(stack, other.org, "other")
        pe, pe_principal = stack.token("pe")
        stack.grant_sql(pe_principal, "project_editor", chain.project)

        # listing is allowed at any folder kind where the caller holds contract:create
        assert ids(await available(stack, chain.project, pe)) == sorted([root_t, org_t, project_t])
        # a template of a folder below the project is not available at the project ...
        # ... but is at the team, where the project editor's grant is inherited
        assert ids(await available(stack, chain.team, pe)) == sorted(
            [root_t, org_t, project_t, team_t]
        )


async def test_who_may_list_pm_without_contract_role_is_403_and_a_stranger_is_404(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        await make(stack, chain.org, "org")
        pm, pm_principal = stack.token("pm")
        stack.grant_sql(pm_principal, "policy_manager", chain.org)
        consumer, consumer_principal = stack.token("consumer")
        stack.grant_sql(consumer_principal, "consumer", chain.org)
        stranger, _ = stack.token("stranger")
        outsider, outsider_principal = stack.token("outsider")
        stack.grant_sql(outsider_principal, "team_editor", other.team)

        assert (await available(stack, chain.team, pm)).status_code == 403
        assert (await preview(stack, chain.team, UNKNOWN, 1, pm)).status_code == 403
        assert (await available(stack, chain.team, consumer)).status_code == 403
        denied = await available(stack, chain.team, stranger)
        unknown = await available(stack, UNKNOWN, stranger)
        assert denied.status_code == 404
        assert stable(denied) == stable(unknown)
        assert (await available(stack, chain.team, outsider)).status_code == 404
        assert (await preview(stack, chain.team, UNKNOWN, 1, stranger)).status_code == 404
        no_token = await stack.client.get(
            f"/api/v1/contracts/folders/{chain.team}/available-templates"
        )
        assert no_token.status_code == 401
        # the Global admin may list at any folder
        assert (await available(stack, chain.team, stack.admin_token)).status_code == 200


async def test_keyset_pages_over_three_templates_have_no_gap_or_duplicate(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        made = [
            await make(stack, chain.org, "one"),
            await make(stack, chain.project, "two"),
            await make(stack, stack.root_id, "three"),
        ]
        ed, ed_principal = stack.token("ed")
        stack.grant_sql(ed_principal, "team_editor", chain.team)

        first = await available(stack, chain.team, ed, limit=2)
        assert len(ids(first)) == 2
        cursor = first.json()["next_cursor"]
        assert cursor
        second = await available(stack, chain.team, ed, limit=2, cursor=cursor)
        assert len(ids(second)) == 1
        assert second.json()["next_cursor"] is None
        assert ids(first) + ids(second) == sorted(made)

        assert (await available(stack, chain.team, ed, limit=0)).status_code == 422
        assert (await available(stack, chain.team, ed, limit=101)).status_code == 422
        assert (await available(stack, chain.team, ed, cursor="!!")).status_code == 422
