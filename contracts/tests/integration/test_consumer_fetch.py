"""Consumer reads of published versions: the versions list, per-version metadata, conditional GET.

A caller who holds only ``contract:read-published`` (a Consumer, RBAC-09) must be able to find and
fetch every published version, and must never learn that a draft, or a draft-only contract,
exists (API-02, API-03, VER-04, D-48; threat T-03-17).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]

UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
SECRET_VERSION = "9.9.9-draft-secret.7"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, *, version: str = "1.0.0") -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        "status: draft\nname: Orders\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def new_odcs_id() -> str:
    return f"urn:dc:consumer-fetch:{uuid.uuid4().hex}"


def versions_url(contract: str, ref: str | None = None, *, metadata: bool = False) -> str:
    base = f"/api/v1/contracts/contracts/{contract}/versions"
    if ref is None:
        return base
    return f"{base}/{ref}/metadata" if metadata else f"{base}/{ref}"


def drafts_url(contract: str, draft: str | None = None) -> str:
    base = f"/api/v1/contracts/contracts/{contract}/drafts"
    return base if draft is None else f"{base}/{draft}"


def stable(response: Any) -> tuple[int, str | None, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, response.headers.get("content-type"), sorted(body.items())


async def publish_version(
    stack: Any, contract: str, odcs_id: str, version: str, *, from_version: str
) -> dict[str, Any]:
    """Branch a draft from ``from_version``, make it declare ``version`` and publish it."""
    created = await stack.client.post(
        drafts_url(contract),
        json={"from_version": from_version, "bump": "none"},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    draft = created.json()["draft"]
    saved = await stack.client.put(
        drafts_url(contract, draft["id"]),
        content=doc(odcs_id, version=version),
        headers={
            **bearer(stack.admin_token),
            "Content-Type": "application/yaml",
            "If-Match": draft["etag"],
        },
    )
    assert saved.status_code == 200, saved.text
    published = await stack.publish(contract, draft["id"], saved.headers["etag"])
    assert published.status_code == 201, published.text
    return dict(published.json())


async def first_version(stack: Any, team: str, odcs_id: str) -> tuple[str, dict[str, Any]]:
    contract, draft, etag = await stack.create_contract(team, doc(odcs_id))
    published = await stack.publish(contract, draft, etag)
    assert published.status_code == 201, published.text
    return contract, dict(published.json())


async def consumer_at(stack: Any, team: str, sub: str = "cs") -> str:
    token, principal = stack.token(sub)
    stack.grant_sql(principal, "consumer", team)
    return token


async def test_a_consumer_lists_versions_with_their_status_and_reads_one_versions_metadata(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_odcs_id()
        contract, first = await first_version(stack, chain.team, odcs_id)
        preview = await publish_version(stack, contract, odcs_id, "1.1.0", from_version="1.0.0")
        assert preview["status"] == "preview"
        consumer = await consumer_at(stack, chain.team)

        listed = await stack.client.get(versions_url(contract), headers=bearer(consumer))
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert [(i["semver"], i["status"]) for i in items] == [
            ("1.1.0", "preview"),
            ("1.0.0", "latest"),
        ]
        for item, published in zip(items, (preview, first), strict=True):
            assert item["version_id"] == published["version_id"]
            assert item["contract_id"] == contract
            assert item["lifecycle"] == "published"
            assert item["deprecated"] is False
            assert item["deprecated_at"] is None
            assert item["sunset_date"] is None
            assert item["content_sha256"] == published["content_sha256"]
            assert item["etag"] == f'"{published["content_sha256"]}"'
            assert item["fetch_url"] == versions_url(contract, item["semver"])
            assert item["media_type"] == "application/yaml"
            assert item["odcs_api_version"] == "v3.2.0"
            assert item["size_bytes"] > 0
            assert item["published_at"] is not None
            assert item["published_by"]

        for ref, expected in (("latest", items[1]), ("1.1.0", items[0]), ("1.0.0", items[1])):
            one = await stack.client.get(
                versions_url(contract, ref, metadata=True), headers=bearer(consumer)
            )
            assert one.status_code == 200, (ref, one.text)
            assert one.json() == expected

        # The fetch_url of a list item is a working address for the document itself.
        fetched = await stack.client.get(items[0]["fetch_url"], headers=bearer(consumer))
        assert fetched.status_code == 200
        assert fetched.headers["etag"] == items[0]["etag"]

        anonymous = await stack.client.get(versions_url(contract))
        assert anonymous.status_code == 401
        assert invariant(stack.contracts_db_url) == []


async def test_the_list_is_ordered_by_semver_precedence_descending(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_odcs_id()
        contract, _first = await first_version(stack, chain.team, odcs_id)
        await publish_version(stack, contract, odcs_id, "1.1.0", from_version="1.0.0")
        for version in ("2.0.0-rc.2", "2.0.0-rc.10", "2.0.0"):
            await publish_version(stack, contract, odcs_id, version, from_version="1.0.0")

        listed = await stack.client.get(versions_url(contract), headers=bearer(stack.admin_token))
        assert listed.status_code == 200, listed.text
        assert [i["semver"] for i in listed.json()["items"]] == [
            "2.0.0",
            "2.0.0-rc.10",
            "2.0.0-rc.2",
            "1.1.0",
            "1.0.0",
        ]
        assert [i["status"] for i in listed.json()["items"]] == [
            "preview",
            "preview",
            "preview",
            "preview",
            "latest",
        ]
        assert invariant(stack.contracts_db_url) == []


async def test_no_existence_oracle_for_a_draft_only_contract(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _draft, _etag = await stack.create_contract(chain.team, doc(new_odcs_id()))
        consumer = await consumer_at(stack, chain.team)
        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", chain.team)
        manager, manager_principal = stack.token("pm")
        stack.grant_sql(manager_principal, "policy_manager", chain.org)

        # A holder of contract:read may see that the contract has no published version.
        for token in (stack.admin_token, reader):
            empty = await stack.client.get(versions_url(contract), headers=bearer(token))
            assert empty.status_code == 200, empty.text
            assert empty.json() == {"items": []}

        # A published-only caller sees exactly what an unknown contract looks like, everywhere.
        for token in (consumer, manager):
            for make_url in (
                lambda c: versions_url(c),
                lambda c: versions_url(c, "latest", metadata=True),
                lambda c: versions_url(c, "latest"),
                lambda c: versions_url(c, "1.0.0", metadata=True),
            ):
                real = await stack.client.get(make_url(contract), headers=bearer(token))
                missing = await stack.client.get(make_url(UNKNOWN), headers=bearer(token))
                assert real.status_code == 404, real.text
                assert stable(real) == stable(missing)
        assert invariant(stack.contracts_db_url) == []


async def test_a_pending_draft_changes_nothing_a_consumer_can_see(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_odcs_id()
        contract, _first = await first_version(stack, chain.team, odcs_id)
        consumer = await consumer_at(stack, chain.team)
        urls = [
            versions_url(contract),
            versions_url(contract, "latest", metadata=True),
            versions_url(contract, "latest"),
        ]
        before = [(await stack.client.get(u, headers=bearer(consumer))).content for u in urls]

        created = await stack.client.post(
            drafts_url(contract),
            json={"from_version": "1.0.0", "bump": "none"},
            headers=bearer(stack.admin_token),
        )
        draft = created.json()["draft"]
        saved = await stack.client.put(
            drafts_url(contract, draft["id"]),
            content=doc(odcs_id, version=SECRET_VERSION),
            headers={
                **bearer(stack.admin_token),
                "Content-Type": "application/yaml",
                "If-Match": draft["etag"],
            },
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        secret_sha = (body.get("draft") or body)["content_sha256"]

        after = [await stack.client.get(u, headers=bearer(consumer)) for u in urls]
        assert [r.content for r in after] == before
        for response in after:
            assert SECRET_VERSION not in response.text
            assert secret_sha not in response.text
            assert secret_sha not in str(response.headers)
        # The draft is not a version under any address, not even its declared semver.
        for ref in (SECRET_VERSION, "9.9.9"):
            for metadata in (False, True):
                hidden = await stack.client.get(
                    versions_url(contract, ref, metadata=metadata), headers=bearer(consumer)
                )
                assert hidden.status_code == 404
        assert invariant(stack.contracts_db_url) == []


async def test_a_ref_that_is_not_a_published_version_is_the_unknown_contract_404_on_metadata(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _first = await first_version(stack, chain.team, new_odcs_id())
        consumer = await consumer_at(stack, chain.team)
        unknown = await stack.client.get(
            versions_url(UNKNOWN, "latest", metadata=True), headers=bearer(consumer)
        )
        assert unknown.status_code == 404
        for ref in ("7.7.7", "1.0", "v1.0.0", "not-a-semver", "LATEST"):
            response = await stack.client.get(
                versions_url(contract, ref, metadata=True), headers=bearer(consumer)
            )
            assert response.status_code == 404, (ref, response.text)
            assert stable(response) == stable(unknown), ref
        # Build metadata only differs: it is the same version.
        twin = await stack.client.get(
            versions_url(contract, "1.0.0+build.9", metadata=True), headers=bearer(consumer)
        )
        assert twin.status_code == 200, twin.text
        assert twin.json()["semver"] == "1.0.0"
        assert invariant(stack.contracts_db_url) == []


async def test_a_policy_manager_and_an_unrelated_consumer_get_the_unknown_404_on_versions(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _first = await first_version(stack, chain.team, new_odcs_id())
        other = await stack.chain()
        manager, manager_principal = stack.token("pm")
        stack.grant_sql(manager_principal, "policy_manager", chain.org)
        elsewhere = await consumer_at(stack, other.team, "elsewhere")
        for token in (manager, elsewhere):
            for make_url in (
                lambda c: versions_url(c),
                lambda c: versions_url(c, "latest", metadata=True),
            ):
                real = await stack.client.get(make_url(contract), headers=bearer(token))
                missing = await stack.client.get(make_url(UNKNOWN), headers=bearer(token))
                assert real.status_code == 404, real.text
                assert stable(real) == stable(missing)
        assert invariant(stack.contracts_db_url) == []


# ---- Task 2: conditional GET (API-03, D-48, T-03-18..T-03-20) ----

CACHE_HEADERS = {"vary": "Accept", "cache-control": "private, no-cache"}


async def published_with_sha(
    stack: Any, odcs_id: str | None = None
) -> tuple[str, str, str, str, bytes]:
    """(team, contract, version sha, ODCS id, bytes) of a contract whose 1.0.0 is published."""
    chain = await stack.chain()
    odcs = odcs_id or new_odcs_id()
    body = doc(odcs)
    contract, draft, etag = await stack.create_contract(chain.team, body)
    published = await stack.publish(contract, draft, etag)
    assert published.status_code == 201, published.text
    return chain.team, contract, published.json()["content_sha256"], odcs, body


async def test_if_none_match_answers_304_with_the_same_cache_headers_and_no_body(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        team, contract, sha, _odcs, body = await published_with_sha(stack)
        consumer = await consumer_at(stack, team)
        url = versions_url(contract, "1.0.0")
        etag = f'"{sha}"'
        for header in (etag, f"W/{etag}", f'"nope", {etag}', f'W/"nope" ,{etag}', "*"):
            response = await stack.client.get(
                url, headers={**bearer(consumer), "If-None-Match": header}
            )
            assert response.status_code == 304, (header, response.text)
            assert response.content == b""
            assert response.headers["etag"] == etag
            for name, value in CACHE_HEADERS.items():
                assert response.headers[name] == value
            assert "max-age" not in response.headers["cache-control"]

        for header in ('"nope"', f'"{sha}-json"', "garbage", ""):
            response = await stack.client.get(
                url, headers={**bearer(consumer), "If-None-Match": header}
            )
            assert response.status_code == 200, (header, response.text)
            assert response.content == body
            assert response.headers["etag"] == etag
        # The same validators work through the latest alias.
        latest = await stack.client.get(
            versions_url(contract, "latest"), headers={**bearer(consumer), "If-None-Match": etag}
        )
        assert latest.status_code == 304
        assert invariant(stack.contracts_db_url) == []


async def test_each_representation_has_its_own_validator(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        _team, contract, sha, _odcs, _body = await published_with_sha(stack)
        url = versions_url(contract, "1.0.0")
        admin = bearer(stack.admin_token)

        as_json = await stack.client.get(
            url, headers={**admin, "Accept": "application/json", "If-None-Match": f'"{sha}"'}
        )
        # The original's tag must not revalidate the JSON conversion (T-03-19).
        assert as_json.status_code == 200, as_json.text
        assert as_json.headers["content-type"] == "application/json"
        assert as_json.headers["etag"] == f'"{sha}-json"'

        again = await stack.client.get(
            url, headers={**admin, "Accept": "application/json", "If-None-Match": f'"{sha}-json"'}
        )
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["etag"] == f'"{sha}-json"'
        assert again.headers["vary"] == "Accept"
        assert again.headers["cache-control"] == "private, no-cache"

        # A client that holds the original's tag and asks for the original gets 304 ...
        original = await stack.client.get(url, headers={**admin, "If-None-Match": f'"{sha}"'})
        assert original.status_code == 304
        # ... and the JSON tag does not match the original.
        mismatch = await stack.client.get(url, headers={**admin, "If-None-Match": f'"{sha}-json"'})
        assert mismatch.status_code == 200
        assert invariant(stack.contracts_db_url) == []


async def test_a_yaml_etag_does_not_revalidate_a_json_request_on_a_json_contract(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        body = (
            '{"apiVersion": "v3.2.0", "kind": "DataContract", "id": "'
            + new_odcs_id()
            + '", "version": "1.0.0", "status": "draft", "name": "Orders"}'
        ).encode()
        contract, draft, etag = await stack.create_contract(chain.team, body, "application/json")
        sha = (await stack.publish(contract, draft, etag)).json()["content_sha256"]
        admin = bearer(stack.admin_token)
        url = versions_url(contract, "1.0.0")

        as_yaml = await stack.client.get(url, headers={**admin, "Accept": "application/yaml"})
        assert as_yaml.status_code == 200, as_yaml.text
        assert as_yaml.headers["etag"] == f'"{sha}-yaml"'
        # Holding the YAML tag and asking for JSON (the stored form) is a 200, not a 304.
        json_again = await stack.client.get(
            url,
            headers={**admin, "Accept": "application/json", "If-None-Match": f'"{sha}-yaml"'},
        )
        assert json_again.status_code == 200
        assert json_again.content == body
        assert json_again.headers["etag"] == f'"{sha}"'
        assert invariant(stack.contracts_db_url) == []


async def test_an_unacceptable_accept_is_406_even_when_if_none_match_matches(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        _team, contract, sha, _odcs, _body = await published_with_sha(stack)
        response = await stack.client.get(
            versions_url(contract, "1.0.0"),
            headers={
                **bearer(stack.admin_token),
                "Accept": "text/html",
                "If-None-Match": f'"{sha}"',
            },
        )
        assert response.status_code == 406, response.text
        assert invariant(stack.contracts_db_url) == []


async def test_a_ref_with_build_metadata_resolves_and_a_two_part_ref_is_the_unknown_404(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        _team, contract, sha, _odcs, body = await published_with_sha(stack)
        admin = bearer(stack.admin_token)
        twin = await stack.client.get(versions_url(contract, "1.0.0+build.7"), headers=admin)
        assert twin.status_code == 200, twin.text
        assert twin.content == body
        # Build metadata never changes the validator.
        revalidated = await stack.client.get(
            versions_url(contract, "1.0.0+build.7"),
            headers={**admin, "If-None-Match": f'"{sha}"'},
        )
        assert revalidated.status_code == 304
        unknown = await stack.client.get(versions_url(UNKNOWN, "1.0.0"), headers=admin)
        short = await stack.client.get(versions_url(contract, "1.0"), headers=admin)
        assert short.status_code == 404
        assert stable(short) == stable(unknown)
        assert invariant(stack.contracts_db_url) == []


async def test_a_conditional_request_never_reveals_what_authorization_hides(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        draft_only, _draft, _etag = await stack.create_contract(chain.team, doc(new_odcs_id()))
        _team, published, sha, _odcs, _body = await published_with_sha(stack)
        consumer = await consumer_at(stack, chain.team)
        manager, manager_principal = stack.token("pm")
        stack.grant_sql(manager_principal, "policy_manager", chain.org)

        # A draft-only contract: "*" answers exactly what an unknown contract answers.
        for ref in ("latest", "1.0.0"):
            real = await stack.client.get(
                versions_url(draft_only, ref), headers={**bearer(consumer), "If-None-Match": "*"}
            )
            missing = await stack.client.get(
                versions_url(UNKNOWN, ref), headers={**bearer(consumer), "If-None-Match": "*"}
            )
            assert real.status_code == 404, real.text
            assert stable(real) == stable(missing)
            assert "etag" not in real.headers

        # No token: 401 whatever the validator says.
        anonymous = await stack.client.get(
            versions_url(published, "1.0.0"), headers={"If-None-Match": f'"{sha}"'}
        )
        assert anonymous.status_code == 401
        # A caller who cannot read published versions is 404, not 304, even with the right tag.
        denied = await stack.client.get(
            versions_url(published, "1.0.0"),
            headers={**bearer(manager), "If-None-Match": f'"{sha}"'},
        )
        unknown = await stack.client.get(
            versions_url(UNKNOWN, "1.0.0"), headers={**bearer(manager), "If-None-Match": "*"}
        )
        assert denied.status_code == 404
        assert stable(denied) == stable(unknown)
        assert invariant(stack.contracts_db_url) == []
