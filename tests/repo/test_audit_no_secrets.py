"""No audit snapshot or detail can carry a secret, a hash, a password or contract content (D-169).

Audit events are built from two kinds of allow-list, and this test pins both statically:

* change events: ``UnitOfWork.snapshot`` copies only a model's ``__audit_fields__`` (identity,
  contracts, validation). No name in any of those tuples may look like a secret, a credential hash,
  a password, an encrypted value, raw or parsed document content or rule bodies. A few names match
  the pattern on purpose and are allowed below, each with its reason;
* denial and access events: the producers emit only their module's ``DETAIL_KEYS``
  (``dc_identity.denials`` and ``dc_contracts.access_events``). Those sets must equal the
  documented allow-lists exactly, so a new key has to be argued for here and in the docs.

The pattern itself is checked against the real sensitive columns of the same models, so it cannot
silently stop matching (a vacuous pattern would pass every allow-list).
"""

from __future__ import annotations

import re
from types import ModuleType

import pytest
from sqlalchemy.orm import DeclarativeBase

pytestmark = pytest.mark.unit

SENSITIVE = re.compile(
    r"secret|hash|password|token|_enc$|raw|document|description_text|^rules$|draft_rules"
    r"|^email$|preferred_username$|webhook_url|^public_id$"
)

# Names that match SENSITIVE but carry no secret or content, with the reason.
ALLOWED: dict[str, str] = {
    "content_sha256": "a SHA-256 digest of the stored bytes: identifies, never reveals, content",
    "draft_content_sha256": "the same digest for a draft (templates, rule sets)",
    "password_set": "a derived boolean (a password exists), never the hash",
    "email_set": "a derived boolean (an email address exists), never the address",
    "preferred_username_set": "a derived boolean, never the Entra username",
}

# Columns that hold a secret or content; the pattern must flag every one of them.
KNOWN_SENSITIVE_COLUMNS = {
    "secret_hash",
    "token_hash",
    "password_hash",
    "raw_document",
    "document",
    "draft_raw_document",
    "draft_document",
    "description_text",
    "rules",
    "draft_rules",
    "secret_enc",
    "previous_enc",
    "email",
    "preferred_username",
    "webhook_url",
    "public_id",
}

DENIAL_DETAIL_KEYS = frozenset({"permission", "outcome", "service", "route"})
DENIAL_OUTCOMES = frozenset({"not_found", "forbidden", "gate_refused", "interactive_required"})
ACCESS_DETAIL_KEYS = frozenset(
    {"contract_id", "version_id", "draft_id", "semver", "ref", "media_type"}
)


def _bases() -> dict[str, type[DeclarativeBase]]:
    from dc_contracts.models import ContractsBase
    from dc_identity.models import IdentityBase
    from dc_validation.models import ValidationBase

    return {"identity": IdentityBase, "contracts": ContractsBase, "validation": ValidationBase}


def _audited_models() -> list[tuple[str, type]]:
    found: list[tuple[str, type]] = []
    for service, base in _bases().items():
        for mapper in base.registry.mappers:
            model = mapper.class_
            if getattr(model, "__audit_fields__", None) is not None:
                found.append((service, model))
    return found


def test_every_service_declares_audited_models() -> None:
    services = {service for service, _ in _audited_models()}
    assert services == {"identity", "contracts", "validation"}


def test_the_pattern_flags_every_known_sensitive_column() -> None:
    columns: set[str] = set()
    for base in _bases().values():
        for mapper in base.registry.mappers:
            columns.update(column.name for column in mapper.columns)
    missing = KNOWN_SENSITIVE_COLUMNS - columns
    assert not missing, f"stale KNOWN_SENSITIVE_COLUMNS (no such column): {sorted(missing)}"
    unflagged = sorted(name for name in KNOWN_SENSITIVE_COLUMNS if not SENSITIVE.search(name))
    assert not unflagged, f"the pattern misses sensitive columns: {unflagged}"


def test_no_audit_allow_list_names_a_secret_hash_password_or_content_column() -> None:
    offending = [
        f"{service}:{model.__name__}.{name}"
        for service, model in _audited_models()
        for name in model.__audit_fields__
        if SENSITIVE.search(name) and name not in ALLOWED
    ]
    assert not offending, f"__audit_fields__ names a sensitive column: {offending}"


def test_every_allowed_exception_is_still_used() -> None:
    used = {name for _, model in _audited_models() for name in model.__audit_fields__}
    stale = sorted(set(ALLOWED) - used)
    assert not stale, f"ALLOWED entries no model uses any more: {stale}"


def test_no_audit_allow_list_names_a_sensitive_column_of_its_own_table() -> None:
    # Belt and braces: whatever the name, a field that is one of the known sensitive columns of
    # its own table is never snapshotted.
    offending = [
        f"{service}:{model.__name__}.{name}"
        for service, model in _audited_models()
        for name in model.__audit_fields__
        if name in KNOWN_SENSITIVE_COLUMNS
    ]
    assert not offending, offending


def _module(name: str) -> ModuleType:
    import importlib

    return importlib.import_module(name)


def test_denial_detail_keys_and_outcomes_are_exactly_the_documented_allow_lists() -> None:
    denials = _module("dc_identity.denials")
    assert frozenset(denials.DETAIL_KEYS) == DENIAL_DETAIL_KEYS
    assert frozenset(denials.OUTCOMES) == DENIAL_OUTCOMES


def test_access_detail_keys_are_exactly_the_documented_allow_list() -> None:
    access = _module("dc_contracts.access_events")
    assert frozenset(access.DETAIL_KEYS) == ACCESS_DETAIL_KEYS
