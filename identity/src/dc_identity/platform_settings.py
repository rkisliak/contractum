"""Platform settings in identity: the store, the checks, the diff and the import (quick 261120-0yc).

D-01: one ``platform_setting`` row per section holding only *stored overrides*; a missing field
means the owning service's Helm/env value applies. D-04: exactly one audited event per effective
write (``settings.updated`` for one section, ``settings.imported`` for an import), ``before`` and
``after`` shaped ``{"<section>": {<stored overrides>}}``, scope ROOT; writing the current values
again is a no-op with no event. D-05: each section has a revision and the ETag
``"<section>-r<revision>"``; an import uses the combined ETag of all sections. D-08: identity's
own reads use a 5 s in-process cache that every local write clears.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dc_auth.principal import Principal
from dc_core.conditional import require_if_match
from dc_core.platform_settings import (
    ALLOWED_HOSTS_MAX,
    SECTIONS,
    check_host_pattern,
    normalise_section,
    notifications_defaults,
)
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from dc_odcs import SUPPORTED_API_VERSIONS
from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dc_identity.models import Folder, PlatformSetting, PlatformSettingSeed
from dc_identity.security_policy import check_signin_change
from dc_identity.sessions import root_folder_id

CACHE_TTL_S = 5.0
_LOCK_KEY = "platform_setting"


@dataclass(frozen=True)
class SecurityContext:
    """What saving a ``security`` section needs to know (quick 261120-0ye, D-37): who is saving
    it, whether an Entra issuer is configured and the env session length (a stored idle timeout
    may not exceed the session length in force)."""

    actor: Principal
    entra_configured: bool
    session_max_default: int


@dataclass(frozen=True)
class StoredSection:
    """One section as stored: overrides, revision (0 = never written) and who changed it last."""

    section: str
    value: dict[str, Any]
    revision: int
    updated_at: datetime | None
    updated_by: str | None

    @property
    def etag(self) -> str:
        return section_etag(self.section, self.revision)


def section_etag(section: str, revision: int) -> str:
    return f'"{section}-r{revision}"'


def combined_etag(stored: Mapping[str, StoredSection]) -> str:
    """``"all-"`` plus the first 16 hex of SHA-256 over the sorted ``section:revision`` list."""
    material = "\n".join(f"{name}:{stored[name].revision}" for name in sorted(stored))
    return f'"all-{hashlib.sha256(material.encode()).hexdigest()[:16]}"'


def _empty(section: str) -> StoredSection:
    return StoredSection(section, {}, 0, None, None)


def _invalid(pointer: str, detail: str, *, slug: str = "validation-error") -> ProblemError:
    return ProblemError(
        422,
        slug,
        "Unprocessable Content",
        detail,
        errors=[{"pointer": pointer, "detail": detail}],
    )


def pointer_of(loc: tuple[Any, ...], base: str = "") -> str:
    parts = (str(p).replace("~", "~0").replace("/", "~1") for p in loc)
    return base + "".join(f"/{p}" for p in parts)


async def load_all(session: AsyncSession) -> dict[str, StoredSection]:
    """Every registered section, a never-written one as an empty revision-0 section."""
    rows = (await session.execute(select(PlatformSetting))).scalars().all()
    stored = {name: _empty(name) for name in SECTIONS}
    for row in rows:
        if row.section in SECTIONS:
            stored[row.section] = StoredSection(
                row.section, dict(row.value), row.revision, row.updated_at, row.updated_by
            )
    return stored


class SettingsStore:
    """Identity's read side: a 5 s cache of every section, cleared on every local write (D-08)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl_s: float = CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = session_factory
        self._ttl = ttl_s
        self._clock = clock
        self._cached: dict[str, StoredSection] | None = None
        self._expires = 0.0

    def clear(self) -> None:
        self._cached = None

    async def all(self) -> dict[str, StoredSection]:
        if self._cached is not None and self._clock() < self._expires:
            return self._cached
        async with self._factory() as session:
            loaded = await load_all(session)
        self._cached = loaded
        self._expires = self._clock() + self._ttl
        return loaded

    async def section(self, name: str) -> StoredSection:
        return (await self.all())[name]


def notifications_default_view(
    service_defaults: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The notifications defaults (D-28): the built-in ones, with contracts' env values for the
    templates, retries, attempts and lead times and mail's ``DC_SMTP_FROM`` laid over them.

    ``service_defaults`` maps a service name to its about ``setting_defaults["notifications"]``;
    a service that did not answer is simply absent and the built-in value stays.
    """
    view = notifications_defaults()
    view["sender"] = {"name": None, "address": None}
    contracts = service_defaults.get("contracts", {})
    for key in view:
        if key != "sender" and key in contracts:
            view[key] = contracts[key]
    mail_sender = service_defaults.get("mail", {}).get("sender")
    if isinstance(mail_sender, Mapping):
        view["sender"] = {"name": mail_sender.get("name"), "address": mail_sender.get("address")}
    return view


def section_defaults(
    section: str,
    *,
    entra_tenant_ids: tuple[str, ...],
    service_defaults: Mapping[str, Mapping[str, Any]] | None = None,
    identity_defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The effective defaults of a section (what applies when no override is stored).

    ``service_defaults`` maps a service to its about ``setting_defaults[<section>]``;
    ``identity_defaults`` is identity's own env view of the ``security`` section (D-43).
    """
    if section == "entra":
        return {
            "default_tenant_id": entra_tenant_ids[0] if len(entra_tenant_ids) == 1 else None,
            "default_owner_folder_id": None,
        }
    if section == "notifications":
        return notifications_default_view(service_defaults or {})
    view: dict[str, Any] = {name: None for name in SECTIONS[section].model_fields}
    if section == "security" and identity_defaults is not None:
        view.update(identity_defaults)
    elif section == "governance":
        for answer in (service_defaults or {}).values():
            view.update({k: v for k, v in answer.items() if k in view})
    elif section == "platform":
        for answer in (service_defaults or {}).values():
            view.update({k: v for k, v in answer.items() if k in view})
    return view


def defaults_source(section: str, *, service_defaults: Mapping[str, Any] | None = None) -> str:
    """Where the defaults of a section come from (D-17): identity's Helm config, the owning
    services (D-28, when at least one answered), or built-in."""
    if section == "entra":
        return "identity"
    if section == "security":
        return "identity"
    if section in ("notifications", "governance", "platform") and service_defaults:
        return "service"
    return "built-in"


def effective_default_tenant(
    stored: StoredSection, entra_tenant_ids: tuple[str, ...]
) -> str | None:
    """E1: the stored default tenant, ignored (``None``) once it is no longer configured (D-09)."""
    tenant = stored.value.get("default_tenant_id")
    return tenant if isinstance(tenant, str) and tenant in entra_tenant_ids else None


async def default_entra_tenant(request: Request) -> str | None:
    """E1 (D-09): the stored default tenant for a register or search call that omits one."""
    state = request.app.state
    return effective_default_tenant(
        await state.platform_settings.section("entra"), state.entra_tenant_ids
    )


async def _check_owner(session: AsyncSession, folder_id: uuid.UUID) -> None:
    """E5 (D-10): ROOT or a non-archived Organization, else 422 ``owner-not-allowed``."""
    folder = (
        await session.execute(select(Folder).where(Folder.id == folder_id))
    ).scalar_one_or_none()
    if (
        folder is None
        or folder.kind not in ("root", "organization")
        or folder.archived_at is not None
    ):
        raise _invalid(
            "/default_owner_folder_id",
            "Only ROOT or an Organization that is not archived can be the default owner.",
            slug="owner-not-allowed",
        )


async def validate_section(
    session: AsyncSession,
    section: str,
    raw: Mapping[str, Any],
    *,
    entra_tenant_ids: tuple[str, ...],
    pointer_base: str = "",
    current: Mapping[str, Any] | None = None,
    security: SecurityContext | None = None,
) -> dict[str, Any]:
    """The normalised overrides of ``raw`` after the section model and identity's own checks.

    Errors are 422 with a JSON pointer (never echoing the input); ``pointer_base`` prefixes it
    for an import. ``current`` is the stored overrides of the section and ``security`` the saving
    admin's context: both are needed to save the ``security`` section (the lockout rules).
    """
    try:
        value = normalise_section(section, raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise _invalid(pointer_of(tuple(first["loc"]), pointer_base), "Invalid setting.") from None
    if section == "entra":
        tenant = value.get("default_tenant_id")
        if tenant is not None and tenant not in entra_tenant_ids:
            raise _invalid(
                f"{pointer_base}/default_tenant_id", "This is not a configured Entra tenant."
            )
        owner = value.get("default_owner_folder_id")
        if owner is not None:
            try:
                await _check_owner(session, uuid.UUID(owner))
            except ProblemError as exc:
                if pointer_base:
                    exc.extensions["errors"] = [
                        {"pointer": f"{pointer_base}/default_owner_folder_id", "detail": exc.detail}
                    ]
                raise
    elif section == "governance":
        version = value.get("default_odcs_api_version")
        if version is not None and version not in SUPPORTED_API_VERSIONS:
            raise _invalid(
                f"{pointer_base}/default_odcs_api_version",
                "This ODCS version is not supported by the platform.",
            )
    elif section == "security" and value != (current or {}):
        if security is None:  # pragma: no cover - every caller passes the context
            raise RuntimeError("saving the security section needs a SecurityContext")
        idle = value.get("idle_timeout_s")
        if (
            idle is not None
            and "session_max_s" not in value
            and idle > security.session_max_default
        ):
            raise _invalid(
                f"{pointer_base}/idle_timeout_s",
                "The idle timeout must not exceed the session length.",
            )
        await check_signin_change(
            session,
            actor=security.actor,
            current=current or {},
            proposed=value,
            entra_configured=security.entra_configured,
            pointer_base=pointer_base,
        )
    return value


def diff(
    current: Mapping[str, Mapping[str, Any]], incoming: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Per-field changes between stored overrides and incoming ones (sections in ``incoming``)."""
    changes: list[dict[str, Any]] = []
    for section in sorted(incoming):
        before, after = current.get(section, {}), incoming[section]
        for field in sorted(set(before) | set(after)):
            if before.get(field) != after.get(field):
                changes.append(
                    {
                        "section": section,
                        "field": field,
                        "before": before.get(field),
                        "after": after.get(field),
                    }
                )
    return changes


async def _lock(session: AsyncSession) -> None:
    """Serialise settings writes (a missing row cannot be locked with FOR UPDATE)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": _LOCK_KEY})


async def _write(session: AsyncSession, stored: StoredSection, value: dict[str, Any]) -> None:
    if stored.revision == 0:
        session.add(PlatformSetting(section=stored.section, value=value, revision=1))
        return
    row = (
        await session.execute(
            select(PlatformSetting).where(PlatformSetting.section == stored.section)
        )
    ).scalar_one()
    row.value = value
    row.revision = stored.revision + 1


async def update_section(
    uow: UnitOfWork,
    store: SettingsStore,
    section: str,
    raw: Mapping[str, Any],
    *,
    if_match: str | None,
    entra_tenant_ids: tuple[str, ...],
    security: SecurityContext | None = None,
) -> StoredSection:
    """Replace one section's overrides: ``If-Match`` (428/412), checks, one ``settings.updated``.

    The current values again is returned with no write and no event.
    """
    session = uow.session
    await _lock(session)
    current = (await load_all(session))[section]
    require_if_match(if_match, current.etag)
    value = await validate_section(
        session,
        section,
        raw,
        entra_tenant_ids=entra_tenant_ids,
        current=current.value,
        security=security,
    )
    if value == current.value:
        return current
    await _write(session, current, value)
    uow.audit(
        "settings.updated",
        "platform_settings",
        section,
        before={section: current.value},
        after={section: value},
        scope_path=[await root_folder_id(session)],
    )
    result = (await load_all(session))[section]
    await uow.commit()
    store.clear()
    return result


WEBHOOK_HOSTS_SEED = "webhook_hosts"


@dataclass(frozen=True)
class SeedResult:
    """What a webhook-host seed call did: ``seeded`` is true only on the one call that ran it."""

    seeded: bool
    added: int
    skipped: int


async def seed_webhook_hosts(uow: UnitOfWork, store: SettingsStore, hosts: list[str]) -> SeedResult:
    """Quick 261120-s0p (item 6): fill the mandatory webhook allow-list once.

    One transaction under the settings write lock: the marker row decides (a later call is a
    no-op, so a host an admin removed is never put back, and two concurrent first calls seed
    once). A non-empty stored list is never widened: only the marker and the event are written.
    Otherwise the valid hosts (``check_host_pattern``; IP literals and junk are skipped),
    deduplicated and sorted, become the list, cut at ``ALLOWED_HOSTS_MAX`` (the overflow counts as
    skipped). One ``settings.webhook_hosts_seeded`` event; a host is never echoed anywhere.
    """
    session = uow.session
    await _lock(session)
    inserted = await session.execute(
        insert(PlatformSettingSeed)
        .values(name=WEBHOOK_HOSTS_SEED, created_by=uow.actor.id, updated_by=uow.actor.id)
        .on_conflict_do_nothing(index_elements=["name"])
        .returning(PlatformSettingSeed.name)
    )
    if inserted.scalar_one_or_none() is None:
        return SeedResult(seeded=False, added=0, skipped=0)
    current = (await load_all(session))["notifications"]
    value = dict(current.value)
    added = skipped = 0
    if not value.get("webhook_allowed_hosts"):
        valid: set[str] = set()
        for host in hosts:
            try:
                valid.add(check_host_pattern(host))
            except ValueError:
                skipped += 1
        kept = sorted(valid)[:ALLOWED_HOSTS_MAX]
        skipped += len(valid) - len(kept)
        # Duplicates of a valid host are neither added nor skipped: they are one entry.
        if kept:
            value["webhook_allowed_hosts"] = kept
            await _write(session, current, value)
            added = len(kept)
    uow.audit(
        "settings.webhook_hosts_seeded",
        "platform_settings",
        "notifications",
        before={"notifications": current.value},
        after={"notifications": value},
        scope_path=[await root_folder_id(session)],
    )
    await uow.commit()
    store.clear()
    return SeedResult(seeded=True, added=added, skipped=skipped)


async def plan_import(
    session: AsyncSession,
    incoming: Mapping[str, Mapping[str, Any]],
    *,
    entra_tenant_ids: tuple[str, ...],
    security: SecurityContext | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, StoredSection], list[dict[str, Any]]]:
    """Validate every incoming section with identity's checks and diff it against the store.

    Returns ``(validated, stored, changes)``; ``changes`` is empty when nothing differs.
    """
    stored = await load_all(session)
    validated: dict[str, dict[str, Any]] = {}
    for section, raw in incoming.items():
        validated[section] = await validate_section(
            session,
            section,
            raw,
            entra_tenant_ids=entra_tenant_ids,
            pointer_base=f"/sections/{section}",
            current=stored[section].value,
            security=security,
        )
    changes = diff({name: s.value for name, s in stored.items()}, validated)
    return validated, stored, changes


async def apply_import(
    uow: UnitOfWork,
    store: SettingsStore,
    incoming: Mapping[str, Mapping[str, Any]],
    *,
    if_match: str | None,
    entra_tenant_ids: tuple[str, ...],
    security: SecurityContext | None = None,
) -> list[dict[str, Any]]:
    """Apply an import in one transaction: combined ``If-Match``, one ``settings.imported``."""
    session = uow.session
    await _lock(session)
    stored = await load_all(session)
    require_if_match(if_match, combined_etag(stored))
    validated, stored, changes = await plan_import(
        session, incoming, entra_tenant_ids=entra_tenant_ids, security=security
    )
    if not changes:
        return []
    changed = sorted({c["section"] for c in changes})
    for section in changed:
        await _write(session, stored[section], validated[section])
    uow.audit(
        "settings.imported",
        "platform_settings",
        "import",
        before={s: stored[s].value for s in changed},
        after={s: validated[s] for s in changed},
        scope_path=[await root_folder_id(session)],
    )
    await uow.commit()
    store.clear()
    return changes
