"""Caller-scoped link facts travel with every validation run (VAL-05, D-153, D-35, D-54).

The facts are what contracts found out about each link of the document: published, draft-only or
not found, where a contract the caller cannot read is exactly as not found as one that does not
exist. A link naming ``/versions/<semver>`` also reports the version and whether it is published.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from dc_auth.authorizer import FolderScope
from dc_auth.principal import Principal
from dc_contracts.link_facts import draft_link_resolver, resolve_link_facts

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]

CONTRACTS = "/api/v1/contracts/contracts"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, *, version: str = "1.0.0", urls: tuple[str, ...] = ()) -> bytes:
    text = (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        "status: draft\nname: Orders\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    )
    if urls:
        text += "authoritativeDefinitions:\n" + "".join(
            f'  - type: businessDefinition\n    url: "{url}"\n' for url in urls
        )
    return text.encode()


def new_id() -> str:
    return f"urn:dc:links:{uuid.uuid4().hex}"


def link(contract: str, suffix: str = "") -> str:
    return f"https://dc.example{CONTRACTS}/{contract}{suffix}"


async def editor(stack: Any, team: str) -> str:
    token, principal_id = stack.token(f"ed-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal_id, "team_editor", team)
    return str(token)


async def published(stack: Any, team: str, *, version: str = "1.0.0") -> str:
    contract, draft, etag = await stack.create_contract(team, doc(new_id(), version=version))
    assert (await stack.publish(contract, draft, etag)).status_code == 201
    return contract


def runs(stack: Any, mode: str) -> list[dict[str, Any]]:
    bodies = [json.loads(r.content) for r in stack.validation_link.requests]
    return [body for body in bodies if body.get("mode") == mode]


async def test_links_resolve_for_the_editor_and_a_hidden_contract_is_a_missing_one(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        mine = await stack.chain()
        other = await stack.chain()
        ed = await editor(stack, mine.team)
        readable = await published(stack, mine.team)
        draft_only, _draft, _etag = await stack.create_contract(mine.team, doc(new_id()))
        hidden = await published(stack, other.team)
        unknown = str(uuid.uuid4())
        urls = (link(readable), link(draft_only), link(hidden), link(unknown))

        contract, draft, etag = await stack.create_contract(
            mine.team, doc(new_id(), urls=urls), token=ed
        )
        save_facts = runs(stack, "save")[-1]["ref_facts"]["contracts"]
        stack.validation_link.requests.clear()
        assert (await stack.publish(contract, draft, etag, token=ed)).status_code == 201
        publish_facts = runs(stack, "publish")[-1]["ref_facts"]["contracts"]

        for facts in (save_facts, publish_facts):
            assert facts[link(readable)]["state"] == "published"
            assert facts[link(draft_only)]["state"] == "unpublished_or_draft_only"
            assert facts[link(hidden)] == {
                "state": "not_found",
                "pinned_version": None,
                "pinned_published": None,
            }
            assert facts[link(unknown)] == facts[link(hidden)]


async def test_a_link_pinning_a_version_reports_whether_that_version_is_published(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)
        target = await published(stack, chain.team)
        # The version that does not exist is pinned on a second contract here; two links to one
        # contract are covered by the IN-04 test below.
        other = await published(stack, chain.team)
        urls = (link(target, "/versions/1.0.0"), link(other, "/versions/1.7.0"))

        await stack.create_contract(chain.team, doc(new_id(), urls=urls), token=ed)

        facts = runs(stack, "save")[-1]["ref_facts"]["contracts"]
        assert facts[urls[0]] == {
            "state": "published",
            "pinned_version": "1.0.0",
            "pinned_published": True,
        }
        assert facts[urls[1]] == {
            "state": "published",
            "pinned_version": "1.7.0",
            "pinned_published": False,
        }


async def test_a_pinned_unpublished_version_is_not_published(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        target, draft, etag = await stack.create_contract(chain.team, doc(new_id()))
        assert (await stack.publish(target, draft, etag)).status_code == 201
        second = await stack.client.post(
            f"{CONTRACTS}/{target}/drafts",
            json={"from_version": "1.0.0", "bump": "minor"},
            headers=bearer(stack.admin_token),
        )
        assert second.status_code == 201, second.text
        body = second.json()["draft"]
        assert (await stack.publish(target, body["id"], body["etag"])).status_code == 201
        unpublished = await stack.client.post(
            f"{CONTRACTS}/{target}/versions/1.1.0/unpublish", headers=bearer(stack.admin_token)
        )
        assert unpublished.status_code == 200, unpublished.text
        url = link(target, "/versions/1.1.0")

        await stack.create_contract(chain.team, doc(new_id(), urls=(url,)))

        fact = runs(stack, "save")[-1]["ref_facts"]["contracts"][url]
        assert fact["state"] == "published"
        assert fact["pinned_version"] == "1.1.0"
        assert fact["pinned_published"] is False


async def test_the_draft_validate_route_sends_link_facts_too(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        target = await published(stack, chain.team)
        url = link(target)
        contract, draft, _etag = await stack.create_contract(chain.team, doc(new_id(), urls=(url,)))
        stack.validation_link.requests.clear()

        response = await stack.client.post(
            f"{CONTRACTS}/{contract}/drafts/{draft}/validate", headers=bearer(stack.admin_token)
        )

        assert response.status_code == 200, response.text
        assert runs(stack, "save")[-1]["ref_facts"]["contracts"][url]["state"] == "published"


async def test_at_most_200_links_are_resolved_and_the_run_still_succeeds(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        urls = tuple(link(str(uuid.uuid4())) for _ in range(201))

        await stack.create_contract(chain.team, doc(new_id(), urls=urls))

        sent = runs(stack, "save")[-1]
        assert len(sent["ref_facts"]["contracts"]) == 200
        assert set(sent["ref_facts"]["contracts"]) == set(urls[:200])


async def test_a_link_to_a_long_build_metadata_version_keeps_the_draft_publishable(
    make_stack: MakeStack,
) -> None:
    """WR-01: a pin of 66+ characters was a 422 for the whole run, reported as a 503."""
    long_build = "1.0.0+build.20261113.sha.0123456789abcdef0123456789abcdef012345678"
    assert len(long_build) == 66
    async with make_stack() as stack:
        chain = await stack.chain()
        target = await published(stack, chain.team, version=long_build)
        url = link(target, f"/versions/{long_build}")

        contract, draft, etag = await stack.create_contract(chain.team, doc(new_id(), urls=(url,)))
        fact = runs(stack, "save")[-1]["ref_facts"]["contracts"][url]
        assert fact["state"] == "published" and fact["pinned_published"] is True
        assert fact["pinned_version"] == long_build

        response = await stack.publish(contract, draft, etag)
        assert response.status_code == 201, response.text


class NoLinkScopes:
    """The real authorizer, except that the scopes link facts read cannot be answered."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def accessible_folders(self, principal: Any, permission: str) -> Any:
        if permission in {"contract:read-metadata", "contract:read-published"}:
            from dc_auth.authorizer import AuthorizerUnavailable

            raise AuthorizerUnavailable("injected failure")
        return await self._inner.accessible_folders(principal, permission)


async def test_an_identity_outage_while_resolving_links_fails_publish_closed(
    make_stack: MakeStack,
) -> None:
    """WR-02: unverifiable links never fail a rule, so publish must not run on them."""
    async with make_stack() as stack:
        chain = await stack.chain()
        url = link(str(uuid.uuid4()))  # a missing target: not_found if identity could answer
        contract, draft, etag = await stack.create_contract(chain.team, doc(new_id(), urls=(url,)))
        state = stack.contracts_app.state
        original = state.authorizer_factory
        state.authorizer_factory = lambda request: NoLinkScopes(original(request))
        stack.validation_link.requests.clear()
        try:
            refused = await stack.publish(contract, draft, etag)
            # Advisory draft validation keeps degrading: the links are only unverifiable.
            advisory = await stack.client.post(
                f"{CONTRACTS}/{contract}/drafts/{draft}/validate",
                headers=bearer(stack.admin_token),
            )
        finally:
            state.authorizer_factory = original

        assert refused.status_code == 503, refused.text
        assert refused.json()["type"] == "urn:dc:problem:service-unavailable"
        assert runs(stack, "publish") == [], "validation is never asked without the facts"
        assert advisory.status_code == 200, advisory.text
        assert runs(stack, "save")[-1].get("ref_facts") in (None, {"contracts": {}})

        # Once identity answers again the same draft publishes normally.
        assert (await stack.publish(contract, draft, etag)).status_code == 201


async def test_two_links_to_one_contract_pinning_two_versions_are_two_facts(
    make_stack: MakeStack,
) -> None:
    """IN-04: the second link used to collapse into the first and was never checked."""
    async with make_stack() as stack:
        chain = await stack.chain()
        target = await published(stack, chain.team)
        urls = (link(target, "/versions/1.0.0"), link(target, "/versions/9.9.9"))

        await stack.create_contract(chain.team, doc(new_id(), urls=urls))

        facts = runs(stack, "save")[-1]["ref_facts"]["contracts"]
        assert facts[urls[0]]["pinned_published"] is True
        assert facts[urls[1]] == {
            "state": "published",
            "pinned_version": "9.9.9",
            "pinned_published": False,
        }


# -- IN-03: the facts are re-checked under the publish lock -----------------------------------


class RacingGate:
    """Wraps the installed gate: after it answers, runs ``act`` once, before publish takes locks."""

    def __init__(self, inner: Any, act: Callable[[], Any]) -> None:
        self._inner = inner
        self._act = act
        self.fired = False

    async def check(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.check(*args, **kwargs)
        if not self.fired:
            self.fired = True
            await self._act()
        return result


async def published_twice(stack: Any, team: str) -> str:
    """A contract with 1.0.0 (Latest) and 1.1.0 (a Preview), both published."""
    target = await published(stack, team)
    second = await stack.client.post(
        f"{CONTRACTS}/{target}/drafts",
        json={"from_version": "1.0.0", "bump": "minor"},
        headers=bearer(stack.admin_token),
    )
    assert second.status_code == 201, second.text
    body = second.json()["draft"]
    assert (await stack.publish(target, body["id"], body["etag"])).status_code == 201
    return target


async def race_publish(stack: Any, contract: str, draft: str, etag: str, act: Any) -> Any:
    state = stack.contracts_app.state
    real_gate = state.publish_gate
    racing = RacingGate(real_gate, act)
    state.publish_gate = racing
    try:
        stack.validation_link.requests.clear()
        raced = await stack.publish(contract, draft, etag)
    finally:
        state.publish_gate = real_gate
    assert racing.fired
    return raced


def draft_row(stack: Any, sql: Any, draft: str) -> dict[str, Any]:
    return dict(
        sql(
            stack.contracts_db_url,
            "SELECT v.state, v.validation_run_id, "
            "(SELECT count(*) FROM contract_reference r WHERE r.source_version_id = v.id) AS refs "
            "FROM contract_version v WHERE v.id = %s",
            draft,
        )[0]
    )


async def test_unpublishing_a_pinned_target_between_the_gate_and_the_lock_is_409_and_a_retry_works(
    make_stack: MakeStack, sql: Any, invariant: Any
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        target = await published_twice(stack, chain.team)
        url = link(target, "/versions/1.1.0")
        contract, draft, etag = await stack.create_contract(chain.team, doc(new_id(), urls=(url,)))

        async def unpublish_target() -> None:
            response = await stack.client.post(
                f"{CONTRACTS}/{target}/versions/1.1.0/unpublish",
                headers=bearer(stack.admin_token),
            )
            assert response.status_code == 200, response.text

        raced = await race_publish(stack, contract, draft, etag, unpublish_target)

        assert raced.status_code == 409, raced.text
        assert raced.json()["type"] == "urn:dc:problem:baseline-changed"
        sent = runs(stack, "publish")[-1]["ref_facts"]["contracts"][url]
        assert sent["pinned_published"] is True
        assert draft_row(stack, sql, draft) == {
            "state": "draft",
            "validation_run_id": None,
            "refs": 0,
        }

        stack.validation_link.requests.clear()
        retried = await stack.publish(contract, draft, etag)
        assert retried.status_code == 201, retried.text
        resent = runs(stack, "publish")[-1]["ref_facts"]["contracts"][url]
        assert resent["pinned_published"] is False
        assert invariant(stack.contracts_db_url) == []


async def test_deleting_a_target_between_the_gate_and_the_lock_is_409_and_a_retry_works(
    make_stack: MakeStack, sql: Any, invariant: Any
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        target = await published(stack, chain.team)
        url = link(target)
        contract, draft, etag = await stack.create_contract(chain.team, doc(new_id(), urls=(url,)))

        async def delete_target() -> None:
            response = await stack.client.delete(
                f"{CONTRACTS}/{target}", headers=bearer(stack.admin_token)
            )
            assert response.status_code == 204, response.text

        raced = await race_publish(stack, contract, draft, etag, delete_target)

        assert raced.status_code == 409, raced.text
        assert raced.json()["type"] == "urn:dc:problem:baseline-changed"
        assert runs(stack, "publish")[-1]["ref_facts"]["contracts"][url]["state"] == "published"
        assert draft_row(stack, sql, draft)["state"] == "draft"

        stack.validation_link.requests.clear()
        retried = await stack.publish(contract, draft, etag)
        assert retried.status_code == 201, retried.text
        assert runs(stack, "publish")[-1]["ref_facts"]["contracts"][url]["state"] == "not_found"
        assert invariant(stack.contracts_db_url) == []


async def test_an_unrelated_change_between_the_gate_and_the_lock_does_not_refuse_the_publish(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        target = await published_twice(stack, chain.team)
        bystander = await published_twice(stack, chain.team)
        url = link(target, "/versions/1.1.0")
        contract, draft, etag = await stack.create_contract(chain.team, doc(new_id(), urls=(url,)))

        async def unpublish_bystander() -> None:
            response = await stack.client.post(
                f"{CONTRACTS}/{bystander}/versions/1.1.0/unpublish",
                headers=bearer(stack.admin_token),
            )
            assert response.status_code == 200, response.text

        raced = await race_publish(stack, contract, draft, etag, unpublish_bystander)

        assert raced.status_code == 201, raced.text


# --- the scopes the facts are read with, called directly (no identity) ----------------------------


class ScopedAuthorizer:
    """Answers ``accessible_folders`` from a fixed map; any other call is a test bug."""

    def __init__(self, scopes: dict[str, Any]) -> None:
        self._scopes = scopes

    async def accessible_folders(self, principal: Any, permission: str) -> Any:
        return self._scopes[permission]


def caller() -> Any:
    return Principal(
        id="fake:scoped",
        type="user",
        display="scoped",
        issuer="https://issuer.test",
        subject="scoped",
        via="jwt",
        scopes=frozenset({"api"}),
    )


async def facts_with(
    stack: Any, org: str, urls: tuple[str, ...], *, metadata: Any, published: Any
) -> dict[str, dict[str, Any]]:
    authorizer = ScopedAuthorizer(
        {"contract:read-metadata": metadata, "contract:read-published": published}
    )
    document = {
        "authoritativeDefinitions": [{"type": "businessDefinition", "url": u} for u in urls]
    }
    async with stack.contracts_app.state.session_factory() as session:
        return await resolve_link_facts(
            session, authorizer, caller(), organization_id=uuid.UUID(org), document=document
        )


async def test_each_scope_alone_decides_what_the_caller_sees_and_an_overlong_link_has_no_fact(
    make_stack: MakeStack,
) -> None:
    everything, nothing = FolderScope(all=True), FolderScope(all=False)
    async with make_stack() as stack:
        chain = await stack.chain()
        target = await published(stack, chain.team)
        draft_only, _draft, _etag = await stack.create_contract(chain.team, doc(new_id()))
        overlong = link(target, "?" + "x" * 2100)
        urls = (link(target), link(draft_only), overlong)

        # Published scope only: the published contract is seen, the draft-only one is not.
        only_published = await facts_with(
            stack, chain.org, urls, metadata=nothing, published=everything
        )
        assert only_published[link(target)]["state"] == "published"
        assert only_published[link(draft_only)]["state"] == "not_found"
        assert overlong not in only_published

        # Metadata scope only: drafts are seen too.
        only_metadata = await facts_with(
            stack, chain.org, urls, metadata=everything, published=nothing
        )
        assert only_metadata[link(target)]["state"] == "published"
        assert only_metadata[link(draft_only)]["state"] == "unpublished_or_draft_only"

        # Neither: every link is one not-found answer, and no query is needed for it.
        blind = await facts_with(stack, chain.org, urls, metadata=nothing, published=nothing)
        assert set(blind) == {link(target), link(draft_only)}
        assert {fact["state"] for fact in blind.values()} == {"not_found"}


async def test_a_draft_resolver_for_a_draft_that_is_gone_resolves_no_facts(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        resolver = draft_link_resolver(
            ScopedAuthorizer({}),  # type: ignore[arg-type]
            caller(),
            uuid.uuid4(),
            uuid.uuid4(),
        )
        async with stack.contracts_app.state.session_factory() as session:
            snapshot = await resolver(session)
        assert snapshot.facts == {}
        assert snapshot.links == ()
