"""Reference facts for cel-2 rules: the ``refs`` binding of a run (D-153, VAL-04, VAL-05).

Rules cannot do I/O, so everything a contract references is resolved before evaluation into
string-keyed maps whose keys are the references as written and whose values are tri-state
(``found``, ``not_found``, ``unverifiable``); only ``not_found`` can fail a rule. Elements and
servers are answered by the document itself (``dc_odcs.element_refs``); contract links arrive in
the run request (validation never calls contracts); people and teams come from identity.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from dc_auth.authorizer import FolderFacts
from dc_auth.principal import Principal
from dc_core.problems import ProblemError
from dc_odcs import (
    MAX_VERSION_CHARS,
    extract_contract_refs,
    reference_inputs,
    resolve_internal,
    string_at,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from dc_validation.report import (
    ValidationLinkCountsOut,
    ValidationRefCountsOut,
    ValidationRefFactsSummaryOut,
)

_log = logging.getLogger(__name__)

LinkState = Literal["published", "unpublished_or_draft_only", "not_found"]

# The longest reference string and the most links a run request may carry facts for.
MAX_LINK_KEY_CHARS = 2048
MAX_LINK_FACTS = 200

_LinkKey = Annotated[str, StringConstraints(min_length=1, max_length=MAX_LINK_KEY_CHARS)]


class ValidationLinkFactIn(BaseModel):
    """What contracts found out about one link: its state and the version a link pins."""

    model_config = ConfigDict(extra="forbid")

    state: LinkState
    # The shared cap (WR-01): a pin is part of a link of at most MAX_LINK_KEY_CHARS, and a smaller
    # literal here made a link to a long CI version a 422 for the whole run (a permanent 503).
    pinned_version: str | None = Field(default=None, max_length=MAX_VERSION_CHARS)
    pinned_published: bool | None = None


class ValidationRefFactsIn(BaseModel):
    """Facts about the document's contract links, sent by contracts (D-153, D-125)."""

    model_config = ConfigDict(extra="forbid")

    contracts: dict[_LinkKey, ValidationLinkFactIn] = Field(
        default_factory=dict, max_length=MAX_LINK_FACTS
    )


RefState = Literal["found", "not_found", "unverifiable"]
_UNVERIFIABLE: RefState = "unverifiable"
_CACHE_MAX_ENTRIES = 10_000


class PrincipalCache:
    """A small per-process TTL memory of identity's definite answers (D-153).

    Keyed by ``(asking principal id, kind, name)``, so one caller's answers are never served to
    another (identity's answer depends on who asks). Only ``found`` and ``not_found`` are kept: an
    ``unverifiable`` answer means identity could not tell, and an outage must not stick. A bounded
    dict (oldest out), not ``cachetools``: no package is added for it.
    """

    def __init__(
        self,
        ttl_s: float,
        *,
        max_entries: int = _CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._clock = clock
        self._entries: dict[tuple[str, str, str], tuple[float, RefState]] = {}

    def get(self, key: tuple[str, str, str]) -> RefState | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry[0] <= self._clock():
            del self._entries[key]
            return None
        return entry[1]

    def put(self, key: tuple[str, str, str], state: RefState) -> None:
        if state == _UNVERIFIABLE or self._ttl <= 0:
            return
        now = self._clock()
        if len(self._entries) >= self._max:
            for stale in [k for k, (expires, _) in self._entries.items() if expires <= now]:
                del self._entries[stale]
            while len(self._entries) >= self._max:
                del self._entries[next(iter(self._entries))]
        self._entries[key] = (now + self._ttl, state)


def _cache_of(app_state: Any) -> PrincipalCache:
    cache = getattr(app_state, "principal_cache", None)
    if not isinstance(cache, PrincipalCache):
        cache = PrincipalCache(app_state.settings.principal_cache_ttl_s)
        app_state.principal_cache = cache
    return cache


async def resolve_people(
    app_state: Any,
    principal: Principal,
    facts: FolderFacts,
    users: tuple[str, ...],
    team_name: str | None,
    authorizer: Any | None,
) -> tuple[dict[str, RefState], dict[str, RefState]]:
    """``(principals, teams)`` maps for the member usernames and the team name (D-153).

    The owning Team folder's slug answers the team name locally (case-insensitive); everything
    else is asked of identity once per distinct name, through the cache. Names past the per-kind
    cap, and every name when identity cannot answer, stay ``unverifiable``.
    """
    cap: int = app_state.settings.max_refs_per_kind
    cache = _cache_of(app_state)
    principals: dict[str, RefState] = {}
    teams: dict[str, RefState] = {}
    ask_users: list[str] = []
    ask_groups: list[str] = []
    for position, name in enumerate(users):
        cached = None if position >= cap else cache.get((principal.id, "user", name))
        principals[name] = cached or _UNVERIFIABLE
        if cached is None and position < cap:
            ask_users.append(name)
    if team_name is not None:
        if facts.kind == "team" and facts.slug.lower() == team_name.lower():
            teams[team_name] = "found"
        else:
            cached = cache.get((principal.id, "group", team_name))
            teams[team_name] = cached or _UNVERIFIABLE
            if cached is None:
                ask_groups.append(team_name)
    if authorizer is None or not (ask_users or ask_groups):
        return principals, teams
    try:
        answer = await authorizer.resolve_principals(principal, ask_users, ask_groups)
    except ProblemError:
        _log.warning("principal_resolve_refused")
        return principals, teams
    for name in ask_users:
        state: RefState = answer.users.get(name, _UNVERIFIABLE)
        principals[name] = state
        cache.put((principal.id, "user", name), state)
    for name in ask_groups:
        state = answer.groups.get(name, _UNVERIFIABLE)
        teams[name] = state
        cache.put((principal.id, "group", name), state)
    return principals, teams


def summarise_refs(refs: dict[str, Any]) -> ValidationRefFactsSummaryOut:
    """Counts per kind and state of a ``refs`` binding; never a reference string (D-157)."""

    def counts(kind: str) -> ValidationRefCountsOut:
        values = list(refs.get(kind, {}).values())
        return ValidationRefCountsOut(
            found=values.count("found"),
            not_found=values.count("not_found"),
            unverifiable=values.count("unverifiable"),
        )

    states = [fact.get("state") for fact in refs.get("contracts", {}).values()]
    return ValidationRefFactsSummaryOut(
        elements=counts("elements"),
        servers=counts("servers"),
        principals=counts("principals"),
        teams=counts("teams"),
        contracts=ValidationLinkCountsOut(
            published=states.count("published"),
            unpublished_or_draft_only=states.count("unpublished_or_draft_only"),
            not_found=states.count("not_found"),
            unverifiable=states.count("unverifiable"),
        ),
    )


def contract_facts(
    document: Any, ref_facts: ValidationRefFactsIn | None, max_refs: int
) -> dict[str, dict[str, Any]]:
    """Every link string of the document is a key; no fact for it means ``unverifiable``."""
    sent = {} if ref_facts is None else ref_facts.contracts
    out: dict[str, dict[str, Any]] = {}
    # Every link string, not every target (IN-04): two links to one contract may pin two versions.
    for link in extract_contract_refs(document, max_refs=max_refs, distinct="raw"):
        raw = string_at(document, link.pointer)
        if raw is None or raw in out:
            continue
        fact = sent.get(raw)
        out[raw] = {"state": "unverifiable"} if fact is None else fact.model_dump(mode="json")
    for raw, fact in sent.items():
        out.setdefault(raw, fact.model_dump(mode="json"))
    return out


async def build_refs(
    app_state: Any,
    principal: Principal,
    facts: FolderFacts,
    document: Any,
    ref_facts: ValidationRefFactsIn | None,
    *,
    authorizer: Any | None = None,
) -> dict[str, Any]:
    """The ``refs`` binding: elements, servers, contracts, principals and teams."""
    settings = app_state.settings
    cap = settings.max_refs_per_kind
    inputs = reference_inputs(document)
    internal = resolve_internal(document, inputs, max_refs=cap)
    principals, teams = await resolve_people(
        app_state, principal, facts, inputs.user_names, inputs.team_name, authorizer
    )
    return {
        "elements": internal["elements"],
        "servers": internal["servers"],
        "contracts": contract_facts(document, ref_facts, cap),
        "principals": principals,
        "teams": teams,
    }


__all__ = [
    "MAX_LINK_FACTS",
    "PrincipalCache",
    "ValidationLinkFactIn",
    "ValidationRefFactsIn",
    "build_refs",
    "contract_facts",
    "resolve_people",
    "summarise_refs",
]
