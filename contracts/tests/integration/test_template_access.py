"""Who may manage which templates (RBAC-06, D-35 to D-37, A4).

Template management belongs to Global admins and Policy Managers at their folder and below.
Everyone else gets 404 on template resources (403 on create where they can read the folder), a
list is trimmed to what the caller may see, and a Global reader sees published templates only.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

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


async def create(stack: Any, folder_id: str, token: str, name: str = "T") -> Any:
    return await stack.client.post(
        f"/api/v1/contracts/folders/{folder_id}/templates",
        params={"name": name},
        content=v32_yaml(),
        headers={**bearer(token), "Content-Type": "application/yaml"},
    )


async def make(stack: Any, folder_id: str, name: str = "T") -> str:
    created = await create(stack, folder_id, stack.admin_token, name)
    assert created.status_code == 201, created.text
    return str(created.json()["template"]["id"])


async def publish(stack: Any, template_id: str, token: str) -> Any:
    return await stack.client.post(f"{TEMPLATES}/{template_id}/publish", headers=bearer(token))


async def listing(stack: Any, token: str, **params: Any) -> Any:
    return await stack.client.get(TEMPLATES, params=params, headers=bearer(token))


def ids(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def stable(response: Any) -> dict[str, Any]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return body


async def test_list_is_trimmed_to_the_managers_folder_and_below(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        org_a = await stack.chain("orga")
        org_b = await stack.chain("orgb")
        root_t = await make(stack, stack.root_id, "root")
        a_org = await make(stack, org_a.org, "a-org")
        a_team = await make(stack, org_a.team, "a-team")
        b_org = await make(stack, org_b.org, "b-org")
        token, principal = stack.token("pm-a")
        stack.grant_sql(principal, "policy_manager", org_a.org)

        assert ids(await listing(stack, token)) == sorted([a_org, a_team])
        everything = ids(await listing(stack, stack.admin_token))
        assert everything == sorted([root_t, a_org, a_team, b_org])

        for sub, role, folder in (
            ("editor", "team_editor", org_a.team),
            ("consumer", "consumer", org_a.team),
            ("reader", "org_reader", org_a.org),
        ):
            other, other_principal = stack.token(sub)
            stack.grant_sql(other_principal, role, folder)
            empty = await listing(stack, other)
            assert empty.status_code == 200
            assert empty.json() == {"items": [], "next_cursor": None}
        stranger, _ = stack.token("stranger")
        assert (await listing(stack, stranger)).json() == {"items": [], "next_cursor": None}


async def test_list_pages_by_keyset_in_ascending_id_order(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = [await make(stack, chain.org, f"t{i}") for i in range(3)]
        first = await listing(stack, stack.admin_token, limit=2)
        assert ids(first) == created[:2]
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        second = await listing(stack, stack.admin_token, limit=2, cursor=cursor)
        assert ids(second) == created[2:]
        assert second.json()["next_cursor"] is None
        assert set(first.json()) == {"items", "next_cursor"}  # no total, no count


async def test_list_folder_filter_is_exact_and_still_trimmed(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        org_a = await stack.chain("orga")
        a_org = await make(stack, org_a.org)
        await make(stack, org_a.team)
        assert ids(await listing(stack, stack.admin_token, folder_id=org_a.org)) == [a_org]
        token, principal = stack.token("pm")
        stack.grant_sql(principal, "policy_manager", org_a.team)
        assert ids(await listing(stack, token, folder_id=org_a.org)) == []


async def test_a_policy_manager_at_a_team_cannot_reach_the_organizations_templates(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        org_template = await make(stack, chain.org)
        token, principal = stack.token("pm-team")
        stack.grant_sql(principal, "policy_manager", chain.team)

        unknown = await stack.client.get(f"{TEMPLATES}/{UNKNOWN}", headers=bearer(token))
        for method, path in (
            ("GET", f"{TEMPLATES}/{org_template}"),
            ("GET", f"{TEMPLATES}/{org_template}/document"),
            ("PUT", f"{TEMPLATES}/{org_template}/draft?name=x"),
            ("POST", f"{TEMPLATES}/{org_template}/publish"),
            ("POST", f"{TEMPLATES}/{org_template}/versions/1/deprecate"),
        ):
            response = await stack.client.request(
                method,
                path,
                content=v32_yaml() if method == "PUT" else None,
                headers={**bearer(token), "Content-Type": "application/yaml"},
            )
            assert response.status_code == 404, (method, path, response.text)
            assert stable(response)["type"] == stable(unknown)["type"]
            assert stable(response)["detail"] == stable(unknown)["detail"]

        assert (await create(stack, chain.team, token)).status_code == 201
        assert (await create(stack, chain.org, token)).status_code == 404


async def test_roles_that_can_read_the_folder_get_403_on_create_and_404_on_the_resource(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        pm_token, pm = stack.token("pm")
        stack.grant_sql(pm, "policy_manager", chain.team)
        created = await create(stack, chain.team, pm_token)
        assert created.status_code == 201, created.text
        template_id = created.json()["template"]["id"]

        for sub, role, folder in (
            ("editor", "team_editor", chain.team),
            ("treader", "team_reader", chain.team),
            ("consumer", "consumer", chain.team),
            ("oreader", "org_reader", chain.org),
        ):
            token, principal = stack.token(sub)
            stack.grant_sql(principal, role, folder)
            denied = await create(stack, chain.team, token)
            assert denied.status_code == 403, (sub, denied.text)
            assert denied.json()["type"] == "urn:dc:problem:forbidden"
            hidden = await stack.client.get(f"{TEMPLATES}/{template_id}", headers=bearer(token))
            assert hidden.status_code == 404, (sub, hidden.text)


async def test_organization_a_manager_never_sees_organization_b_templates(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        org_a = await stack.chain("orga")
        org_b = await stack.chain("orgb")
        b_template = await make(stack, org_b.org)
        a_template = await make(stack, org_a.org)
        token, principal = stack.token("pm-a")
        stack.grant_sql(principal, "policy_manager", org_a.org)

        assert (
            await stack.client.get(f"{TEMPLATES}/{b_template}", headers=bearer(token))
        ).status_code == 404
        assert (
            await stack.client.get(f"{TEMPLATES}/{a_template}", headers=bearer(token))
        ).status_code == 200
        assert b_template not in ids(await listing(stack, token))
        assert (await create(stack, org_b.org, token)).status_code == 404


async def test_global_reader_sees_published_templates_but_never_a_draft(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        pm_token, pm = stack.token("pm")
        stack.grant_sql(pm, "policy_manager", chain.org)
        gr_token, gr = stack.token("gr")
        stack.grant_sql(gr, "global_reader", stack.root_id)

        published = await make(stack, chain.org, "published")
        assert (await publish(stack, published, pm_token)).status_code == 201
        put = await stack.client.put(
            f"{TEMPLATES}/{published}/draft",
            content=changed(),
            headers={**bearer(pm_token), "Content-Type": "application/yaml"},
        )
        assert put.status_code == 200, put.text
        assert (await publish(stack, published, pm_token)).status_code == 201
        deprecated = await stack.client.post(
            f"{TEMPLATES}/{published}/versions/1/deprecate", headers=bearer(pm_token)
        )
        assert deprecated.status_code == 200
        draft_only = await make(stack, chain.org, "draft only")

        assert ids(await listing(stack, gr_token)) == [published]  # the draft-only one is absent

        one = await stack.client.get(f"{TEMPLATES}/{published}", headers=bearer(gr_token))
        assert one.status_code == 200, one.text
        body = one.json()
        assert body["draft_redacted"] is True
        for field in (
            "draft_api_version",
            "draft_media_type",
            "draft_content_sha256",
            "draft_size_bytes",
        ):
            assert body[field] is None, field
        assert body["latest_version_no"] == 2

        listed = await stack.client.get(
            f"{TEMPLATES}/{published}/versions", headers=bearer(gr_token)
        )
        assert listed.status_code == 200
        assert [(v["version_no"], v["deprecated"]) for v in listed.json()["items"]] == [
            (1, True),
            (2, False),
        ]
        version = await stack.client.get(
            f"{TEMPLATES}/{published}/versions/2/document", headers=bearer(gr_token)
        )
        assert version.status_code == 200
        assert version.content == changed()

        # the draft document and every write route are 404, as is a never-published template
        unknown = await stack.client.get(f"{TEMPLATES}/{UNKNOWN}", headers=bearer(gr_token))
        draft = await stack.client.get(
            f"{TEMPLATES}/{published}/document", headers=bearer(gr_token)
        )
        assert draft.status_code == 404
        for path in (
            f"{TEMPLATES}/{draft_only}",
            f"{TEMPLATES}/{draft_only}/versions",
            f"{TEMPLATES}/{draft_only}/versions/1/document",
        ):
            hidden = await stack.client.get(path, headers=bearer(gr_token))
            assert hidden.status_code == 404, path
            assert stable(hidden)["type"] == stable(unknown)["type"]
            assert stable(hidden)["detail"] == stable(unknown)["detail"]
        writes = (
            await stack.client.put(
                f"{TEMPLATES}/{published}/draft",
                content=v32_yaml(),
                headers={**bearer(gr_token), "Content-Type": "application/yaml"},
            ),
            await publish(stack, published, gr_token),
            await stack.client.post(
                f"{TEMPLATES}/{published}/versions/2/deprecate", headers=bearer(gr_token)
            ),
            await create(stack, chain.org, gr_token),
        )
        assert [r.status_code for r in writes[:3]] == [404, 404, 404]
        assert writes[3].status_code == 403  # a Global reader can read the folder (D-35)

        manager = await stack.client.get(f"{TEMPLATES}/{published}", headers=bearer(pm_token))
        assert manager.json()["draft_redacted"] is False
        assert manager.json()["draft_content_sha256"] is not None


async def test_published_only_reader_sees_the_published_name_and_attribution_not_draft_edits(
    make_stack: MakeStack,
) -> None:
    """WR-04: a rename or edit of the draft is invisible until it is published."""
    async with make_stack() as stack:
        chain = await stack.chain()
        pm_token, pm = stack.token("pm")
        stack.grant_sql(pm, "policy_manager", chain.org)
        gr_token, gr = stack.token("gr")
        stack.grant_sql(gr, "global_reader", stack.root_id)
        other_token, other = stack.token("pm2")
        stack.grant_sql(other, "policy_manager", chain.org)

        template_id = await make(stack, chain.org, "Published name")
        assert (await publish(stack, template_id, pm_token)).status_code == 201
        version = (
            await stack.client.get(f"{TEMPLATES}/{template_id}/versions", headers=bearer(gr_token))
        ).json()["items"][0]

        # a second manager renames the draft and edits it; nothing is published
        renamed = await stack.client.put(
            f"{TEMPLATES}/{template_id}/draft",
            params={"name": "Secret planned rename"},
            content=changed(),
            headers={**bearer(other_token), "Content-Type": "application/yaml"},
        )
        assert renamed.status_code == 200, renamed.text

        reader_one = await stack.client.get(f"{TEMPLATES}/{template_id}", headers=bearer(gr_token))
        reader_list = await listing(stack, gr_token)
        manager_one = await stack.client.get(f"{TEMPLATES}/{template_id}", headers=bearer(pm_token))
        assert reader_one.status_code == 200 and reader_list.status_code == 200
        listed = reader_list.json()["items"][0]
        for body in (reader_one.json(), listed):
            assert body["draft_redacted"] is True
            assert body["name"] == "Published name"
            assert body["updated_by"] == version["created_by"]
            assert body["updated_at"] == version["created_at"]
            assert "Secret planned rename" not in str(body)
        # the manager still sees the live draft name and who edited it
        assert manager_one.json()["name"] == "Secret planned rename"
        assert manager_one.json()["updated_by"] != version["created_by"]

        # publishing the rename makes it visible to the reader
        assert (await publish(stack, template_id, pm_token)).status_code == 201
        after = await stack.client.get(f"{TEMPLATES}/{template_id}", headers=bearer(gr_token))
        assert after.json()["name"] == "Secret planned rename"
