"""Reference facts: people, teams, contract links, caps, cache and the summary (plan 06-07)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from dc_auth.authorizer import FolderFacts
from dc_auth.principal import Principal
from dc_auth.testing import FakePrincipalDirectory
from dc_core.problems import ProblemError
from dc_validation.facts import (
    PrincipalCache,
    ValidationLinkFactIn,
    ValidationRefFactsIn,
    build_refs,
    summarise_refs,
)
from dc_validation.settings import ValidationSettings
from pydantic import ValidationError

pytestmark = pytest.mark.unit

LINK = "https://dc.example/api/v1/contracts/contracts/0198aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"


class Counting(FakePrincipalDirectory):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls: list[tuple[list[str], list[str]]] = []

    async def resolve_principals(self, principal: Any, users: Any, groups: Any) -> Any:
        self.calls.append((list(users), list(groups)))
        return await super().resolve_principals(principal, users, groups)


def state(**settings: Any) -> Any:
    return SimpleNamespace(settings=ValidationSettings(**settings))


def principal(pid: str = "fake:ed") -> Principal:
    return Principal(id=pid, type="user", display="Ed", issuer="x", subject=pid, via="jwt")


def folder(slug: str = "payments", kind: str = "team") -> FolderFacts:
    return FolderFacts(
        id=uuid.uuid4(),
        kind=kind,  # type: ignore[arg-type]
        slug=slug,
        path=slug,
        ancestor_ids=(),
        organization_id=None,
        archived=False,
    )


def doc(**extra: Any) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": "id"}]}], **extra}


def team(*names: str, name: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"members": [{"username": n} for n in names]}
    if name is not None:
        body["name"] = name
    return {"team": body}


async def test_members_are_found_or_not_found_by_identity() -> None:
    directory = Counting(users=["alice"])
    refs = await build_refs(
        state(), principal(), folder(), doc(**team("alice", "ghost")), None, authorizer=directory
    )
    assert refs["principals"] == {"alice": "found", "ghost": "not_found"}


async def test_an_identity_outage_leaves_every_name_unverifiable() -> None:
    directory = Counting(users=["alice"], outage=True)
    refs = await build_refs(
        state(),
        principal(),
        folder(),
        doc(**team("alice", "ghost", name="x")),
        None,
        authorizer=directory,
    )
    assert refs["principals"] == {"alice": "unverifiable", "ghost": "unverifiable"}
    assert refs["teams"] == {"x": "unverifiable"}


async def test_no_authorizer_means_unverifiable_never_not_found() -> None:
    refs = await build_refs(state(), principal(), folder(), doc(**team("alice", name="x")), None)
    assert refs["principals"] == {"alice": "unverifiable"}
    assert refs["teams"] == {"x": "unverifiable"}


async def test_the_team_folder_slug_answers_the_team_name_locally_in_any_case() -> None:
    directory = Counting()
    refs = await build_refs(
        state(),
        principal(),
        folder("payments"),
        doc(**team(name="PayMents")),
        None,
        authorizer=directory,
    )
    assert refs["teams"] == {"PayMents": "found"}
    assert directory.calls == []


async def test_another_team_name_is_a_local_group_or_not_found() -> None:
    directory = Counting(groups=["data-eng"])
    known = await build_refs(
        state(), principal(), folder(), doc(**team(name="Data-Eng")), None, authorizer=directory
    )
    unknown = await build_refs(
        state(),
        principal("fake:other"),
        folder(),
        doc(**team(name="nobody")),
        None,
        authorizer=directory,
    )
    assert known["teams"] == {"Data-Eng": "found"}
    assert unknown["teams"] == {"nobody": "not_found"}


async def test_the_slug_shortcut_applies_only_to_a_team_folder() -> None:
    directory = Counting()
    refs = await build_refs(
        state(),
        principal(),
        folder("payments", kind="project"),
        doc(**team(name="payments")),
        None,
        authorizer=directory,
    )
    assert refs["teams"] == {"payments": "not_found"}


async def test_a_second_run_inside_the_ttl_does_not_ask_identity_again() -> None:
    directory = Counting(users=["alice"])
    app = state()
    document = doc(**team("alice", "ghost"))
    await build_refs(app, principal(), folder(), document, None, authorizer=directory)
    again = await build_refs(app, principal(), folder(), document, None, authorizer=directory)
    assert len(directory.calls) == 1
    assert again["principals"] == {"alice": "found", "ghost": "not_found"}


async def test_the_cache_is_per_principal_and_expires() -> None:
    directory = Counting(users=["alice"])
    app = state(principal_cache_ttl_s=0)
    document = doc(**team("alice"))
    await build_refs(app, principal(), folder(), document, None, authorizer=directory)
    await build_refs(app, principal(), folder(), document, None, authorizer=directory)
    assert len(directory.calls) == 2
    app = state()
    await build_refs(app, principal("fake:a"), folder(), document, None, authorizer=directory)
    await build_refs(app, principal("fake:b"), folder(), document, None, authorizer=directory)
    assert len(directory.calls) == 4


async def test_an_unverifiable_answer_is_never_cached() -> None:
    down = Counting(users=["alice"], outage=True)
    up = Counting(users=["alice"])
    app = state()
    document = doc(**team("alice"))
    await build_refs(app, principal(), folder(), document, None, authorizer=down)
    refs = await build_refs(app, principal(), folder(), document, None, authorizer=up)
    assert refs["principals"] == {"alice": "found"}


async def test_contract_links_come_only_from_ref_facts() -> None:
    document = doc(schema=[{"name": "o", "relationships": [{"to": LINK}]}])
    sent = ValidationRefFactsIn(
        contracts={LINK: ValidationLinkFactIn(state="published", pinned_version="1.2.0")}
    )
    with_facts = await build_refs(state(), principal(), folder(), document, sent)
    without = await build_refs(state(), principal(), folder(), document, None)
    assert with_facts["contracts"][LINK] == {
        "state": "published",
        "pinned_version": "1.2.0",
        "pinned_published": None,
    }
    assert without["contracts"] == {LINK: {"state": "unverifiable"}}


async def test_two_links_to_one_contract_are_two_keys() -> None:
    """IN-04: the second link pins another version; without its fact it is unverifiable."""
    first, second = f"{LINK}/versions/1.0.0", f"{LINK}/versions/9.9.9"
    document = doc(authoritativeDefinitions=[{"url": first}, {"url": second}])
    sent = ValidationRefFactsIn(
        contracts={
            first: ValidationLinkFactIn(
                state="published", pinned_version="1.0.0", pinned_published=True
            )
        }
    )
    refs = await build_refs(state(), principal(), folder(), document, sent)
    assert refs["contracts"][first]["pinned_published"] is True
    assert refs["contracts"][second] == {"state": "unverifiable"}


async def test_250_distinct_elements_resolve_200_and_leave_50_unverifiable() -> None:
    document = doc(slaProperties=[{"property": "p", "element": f"orders.c{i}"} for i in range(250)])
    refs = await build_refs(state(), principal(), folder(), document, None)
    values = list(refs["elements"].values())
    assert len(values) == 250
    assert values.count("not_found") == 200
    assert values.count("unverifiable") == 50


async def test_names_beyond_the_cap_are_unverifiable_and_not_sent() -> None:
    directory = Counting()
    names = [f"user{i}" for i in range(15)]
    refs = await build_refs(
        state(max_refs_per_kind=10),
        principal(),
        folder(),
        doc(**team(*names)),
        None,
        authorizer=directory,
    )
    assert list(refs["principals"].values()).count("unverifiable") == 5
    assert len(directory.calls[0][0]) == 10


def test_ref_facts_over_200_entries_or_with_an_unknown_key_are_refused() -> None:
    many = {f"https://x/{i}": {"state": "published"} for i in range(201)}
    with pytest.raises(ValidationError):
        ValidationRefFactsIn.model_validate({"contracts": many})
    with pytest.raises(ValidationError):
        ValidationRefFactsIn.model_validate({"contracts": {}, "extra": 1})
    with pytest.raises(ValidationError):
        ValidationRefFactsIn.model_validate({"contracts": {"x" * 2049: {"state": "published"}}})
    with pytest.raises(ValidationError):
        ValidationRefFactsIn.model_validate({"contracts": {"x": {"state": "weird"}}})


async def test_the_summary_counts_per_kind_and_state_and_holds_no_names() -> None:
    directory = Counting(users=["alice"])
    document = doc(
        slaProperties=[{"property": "p", "element": "orders.id,orders.zzz"}],
        **team("alice", "ghost"),
    )
    refs = await build_refs(state(), principal(), folder(), document, None, authorizer=directory)

    summary = summarise_refs(refs)

    assert summary.elements.model_dump() == {"found": 1, "not_found": 1, "unverifiable": 0}
    assert summary.principals.model_dump() == {"found": 1, "not_found": 1, "unverifiable": 0}
    assert summary.contracts.model_dump() == {
        "published": 0,
        "unpublished_or_draft_only": 0,
        "not_found": 0,
        "unverifiable": 0,
    }
    dumped = summary.model_dump_json()
    assert "alice" not in dumped and "orders" not in dumped


async def test_a_cached_team_answer_is_reused_without_asking_identity() -> None:
    directory = Counting(groups=["finance"])
    app = state()
    document = doc(**team(name="finance"))
    await build_refs(app, principal(), folder(), document, None, authorizer=directory)
    again = await build_refs(app, principal(), folder(), document, None, authorizer=directory)
    assert directory.calls == [([], ["finance"])]
    assert again["teams"] == {"finance": "found"}


class Refusing(Counting):
    async def resolve_principals(self, principal: Any, users: Any, groups: Any) -> Any:
        self.calls.append((list(users), list(groups)))
        raise ProblemError(403, "forbidden", "Forbidden", "No.")


async def test_a_refused_principal_lookup_leaves_every_name_unverifiable() -> None:
    refusing = Refusing(users=["alice"], groups=["finance"])
    document = doc(**team("alice", name="finance"))
    refs = await build_refs(state(), principal(), folder(), document, None, authorizer=refusing)
    assert refusing.calls == [(["alice"], ["finance"])]
    assert refs["principals"] == {"alice": "unverifiable"}
    assert refs["teams"] == {"finance": "unverifiable"}


async def test_the_same_link_string_twice_is_one_key() -> None:
    document = doc(authoritativeDefinitions=[{"url": LINK}, {"url": LINK}])
    refs = await build_refs(state(), principal(), folder(), document, None)
    assert refs["contracts"] == {LINK: {"state": "unverifiable"}}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_an_expired_cache_entry_is_dropped_on_read() -> None:
    clock = Clock()
    cache = PrincipalCache(10, clock=clock)
    cache.put(("p", "user", "alice"), "found")
    assert cache.get(("p", "user", "alice")) == "found"
    clock.now += 10
    assert cache.get(("p", "user", "alice")) is None
    clock.now -= 10  # the entry is gone, not merely hidden by the clock
    assert cache.get(("p", "user", "alice")) is None


def test_a_full_cache_drops_stale_entries_first_then_the_oldest() -> None:
    clock = Clock()
    cache = PrincipalCache(10, max_entries=2, clock=clock)
    cache.put(("p", "user", "old"), "found")
    clock.now += 5
    cache.put(("p", "user", "mid"), "not_found")
    clock.now += 6  # "old" is stale now, "mid" is not
    cache.put(("p", "user", "new"), "found")
    assert cache.get(("p", "user", "mid")) == "not_found"  # only the stale entry made room
    assert cache.get(("p", "user", "new")) == "found"
    cache.put(("p", "user", "newest"), "found")  # nothing stale: the oldest live entry goes
    assert cache.get(("p", "user", "mid")) is None
    assert cache.get(("p", "user", "new")) == "found"
    assert cache.get(("p", "user", "newest")) == "found"
