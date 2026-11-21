"""PrincipalGate: every decision first proves the principal is live and, for Entra, registered.

D-57, D-72, D-73, D-77. The gate sits in front of the PDP (``LocalAuthorizer``): no decision
(``authz:check``, ``password:rotate`` and ``accessible_folders`` included) is made for a principal
the gate refuses. Authority still comes only from identity's own tables; the gate decides
admission, never a role.

* A local user must exist and be active (the access JWT outlives a deactivation, so liveness is
  read on every decision), and its JWT must not predate the user's ``sessions_valid_after`` cutoff
  (set at reactivation, D-75): a JWT whose verified iat is at or before it is a 401. The cutoff
  only narrows; nothing beyond iat is read from the token.
* An Entra user or service principal must exist and be enabled in the directory (asked first,
  outside any database session), and must either hold an active, directly registered registration
  of its own kind for its ``(tenant_id, object_id)`` or be a transitive member of an active
  registered Entra group of the same tenant. Registration grants no role; the directory's group
  GUIDs become ``Grantees.entra_groups`` so grants made to those groups apply. The same
  ``sessions_valid_after`` cutoff applies to the Entra user's row and to a service principal's row
  (direct or just-in-time), written at reactivation (D-75).
* After admission (never before, never for a refusal) the gate writes the just-in-time records in
  their own units of work: the ``entra_users`` row of a user and, for a service principal admitted
  only through a group, a ``directly_registered = false`` row owned by its oldest admitting group
  (``entra.py``). Our own ``is_active`` flag on either row answers 401, so a deactivation sticks
  although the directory still says enabled and a group still admits (D-58, D-75). A JIT row
  never admits on its own.
* A personal access token (``via == "pat"``) is never admitted on its own: the gate loads the
  credential live (not revoked, not expired), proves its owner is the principal id the resolver
  built, and re-proves the owner's liveness and admission on every request exactly as for a
  sign-in (a local owner must be active; an Entra owner goes through the same directory lookup,
  own flag and admission checks, but a PAT never creates just-in-time records). The PAT therefore
  never holds more than its owner currently holds (D-65, D-66, D-72).
* A service-account key (``via == "api_key"``) is loaded live the same way: not revoked, not
  expired, of kind ``service_account_key``, and its account must be the ``sa:<uuid>`` the resolver
  built and must be ``is_active`` (T-04-30). The principal is the account itself; it holds only
  what grants and local-group memberships give ``sa:<uuid>`` (D-71, D-72, D-78).
* Principals of generic OIDC issuers keep their Phase 1-3 behaviour (flagged assumption A12).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from dc_auth.verify import unauthenticated
from dc_core.problems import ProblemError
from sqlalchemy import func, select

from dc_identity.directory import DirectoryClient, DirectoryEntry, DirectoryUnavailable
from dc_identity.models import ApiCredential, EntraRegistration, EntraUser, ServiceAccount, User
from dc_identity.principal_ids import (
    entra_principal_id,
    grantee_type,
    local_principal_id,
    parse_principal_id,
    service_account_principal_id,
)

if TYPE_CHECKING:
    from dc_auth.principal import Principal
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dc_identity.credentials import CredentialUsageRecorder


def issued_before_cutoff(principal: Principal, cutoff: datetime | None) -> bool:
    """True when a JWT principal predates its account's session cutoff (D-75).

    ``iat`` has whole-second precision, so a JWT from the cutoff's own second is refused too (fail
    closed). A JWT without a verified iat is refused once a cutoff exists. A PAT or key principal
    is never judged here (revocation and liveness cover them) and no cutoff means nothing to
    compare.
    """
    if principal.via != "jwt" or cutoff is None:
        return False
    return principal.issued_at is None or principal.issued_at <= int(cutoff.timestamp())


@dataclass(frozen=True)
class ResolvedPrincipal:
    """What the gate learned: the grantee identity every grant and membership is matched on."""

    grantee_type: str
    grantee_id: str
    entra_groups: tuple[str, ...] = ()
    read_only: bool = False


# Per-request hand-off from ``resolve`` to ``access.grantees_for`` (same task). ``resolve`` never
# reads it, so no decision is cached: every check stays live (D-72).
_RESOLVED: ContextVar[tuple[tuple[str, str, str | None], ResolvedPrincipal] | None] = ContextVar(
    "dc_identity_resolved_principal", default=None
)


def _key(principal: Principal) -> tuple[str, str, str | None]:
    return (principal.id, str(principal.via), principal.credential_id)


def resolution_for(principal: Principal) -> ResolvedPrincipal | None:
    """The gate's answer for ``principal`` in this request, or None when it has not resolved."""
    held = _RESOLVED.get()
    if held is None or held[0] != _key(principal):
        return None
    return held[1]


async def resolved_for_request(request: Request, principal: Principal) -> ResolvedPrincipal:
    """The gate's answer for this request (``require`` already ran it), resolving if absent."""
    found = resolution_for(principal)
    if found is not None:
        return found
    resolved: ResolvedPrincipal = await request.app.state.principal_gate.resolve(principal)
    return resolved


def not_registered() -> ProblemError:
    return ProblemError(
        403,
        "principal-not-registered",
        "Forbidden",
        "This identity is not registered for this service.",
    )


def sign_in_method_disabled(method: str = "Microsoft") -> ProblemError:
    """403 ``sign-in-method-disabled`` (quick 261120-0ye, D-37): the method was turned off."""
    return ProblemError(
        403,
        "sign-in-method-disabled",
        "Forbidden",
        f"{method} sign-in is turned off for this platform.",
    )


def directory_unavailable() -> ProblemError:
    return ProblemError(
        503,
        "service-unavailable",
        "Service Unavailable",
        "The directory is temporarily unavailable.",
    )


@dataclass(frozen=True)
class _EntraAdmission:
    """What an admitted Entra principal's checks found (all read in one session)."""

    entry: DirectoryEntry
    group: EntraRegistration | None
    user_row: EntraUser | None
    sp_row: EntraRegistration | None


class PrincipalGate:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        directory: DirectoryClient,
        *,
        service: str = "identity",
        usage: CredentialUsageRecorder | None = None,
        microsoft_signin: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._directory = directory
        self._service = service
        self._usage = usage
        # Quick 261120-0ye (S1): answers "may an Entra user sign in with Microsoft right now?".
        # None means no setting store is wired (a bare gate): Microsoft sign-in stays on.
        self._microsoft_signin = microsoft_signin

    async def resolve(self, principal: Principal) -> ResolvedPrincipal:
        """The principal's grantee identity, or a ProblemError when it may not act at all."""
        resolved = await self._resolve(principal)
        _RESOLVED.set((_key(principal), resolved))
        return resolved

    async def _resolve(self, principal: Principal) -> ResolvedPrincipal:
        if principal.via == "pat":
            return await self._resolve_pat(principal)
        if principal.via == "api_key":
            return await self._resolve_api_key(principal)
        ref = parse_principal_id(principal.id)
        if ref is None:
            return ResolvedPrincipal(grantee_type(principal), principal.id)
        if ref.kind == "local_user":
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(User.is_active, User.sessions_valid_after).where(
                            User.id == ref.local_id
                        )
                    )
                ).one_or_none()
            if row is None or row.is_active is not True:
                raise unauthenticated()
            if issued_before_cutoff(principal, row.sessions_valid_after):
                raise unauthenticated()
            return ResolvedPrincipal("user", principal.id)
        if ref.kind == "entra" and principal.type in ("user", "service_principal"):
            assert ref.tenant_id is not None and ref.object_id is not None  # noqa: S101
            # S1: an Entra *user* on an Entra-issued JWT is a Microsoft sign-in. Service
            # principals, PATs and keys are unaffected (they are not sign-ins).
            if (
                principal.type == "user"
                and principal.via == "jwt"
                and self._microsoft_signin is not None
                and not await self._microsoft_signin()
            ):
                raise sign_in_method_disabled()
            admitted = await self._admit_entra(
                principal, principal.type, ref.tenant_id, ref.object_id
            )
            await self._record(
                principal,
                ref.tenant_id,
                ref.object_id,
                admitted.entry,
                admitted.user_row,
                admitted.sp_row,
                admitted.group,
            )
            return ResolvedPrincipal(
                principal.type, principal.id, tuple(sorted(admitted.entry.group_ids))
            )
        # An sa: id is minted only by identity's key resolver (via "api_key", handled above): an
        # sa: id on any other path, or an Entra id with a type no Entra token carries, fails closed.
        raise unauthenticated()

    async def _admit_entra(
        self, principal: Principal, kind: str, tenant_id: str, object_id: str
    ) -> _EntraAdmission:
        """Directory lookup, admission by registration and our own active flag (D-57, D-58, D-65).

        Raises 401 (not in the directory, disabled, or deactivated here), 403
        ``principal-not-registered`` or 503; writes nothing.
        """
        try:
            entry = await self._directory.lookup(tenant_id, kind, object_id)  # type: ignore[arg-type]
        except DirectoryUnavailable as exc:
            raise directory_unavailable() from exc
        if not entry.found or not entry.account_enabled:
            raise unauthenticated()
        async with self._session_factory() as session:
            direct = await self._directly_registered(session, kind, tenant_id, object_id)
            group = await self._admitting_group(session, tenant_id, entry.group_ids)
            user_row = (
                await self._user_row(session, tenant_id, object_id) if kind == "user" else None
            )
            sp_row = (
                await self._service_principal_row(session, tenant_id, object_id)
                if kind == "service_principal"
                else None
            )
        if not direct and group is None:
            raise not_registered()
        # Our own active flag (D-58, D-75): it ends access although the directory still says
        # enabled and a registered group still admits the principal.
        if (user_row is not None and not user_row.is_active) or (
            sp_row is not None and not sp_row.is_active
        ):
            raise unauthenticated()
        # Session cutoff (D-75): a JWT issued at or before a reactivation stays refused. Only a
        # JWT is judged (a PAT is covered by its own revocation); the cutoff never admits.
        if user_row is not None and issued_before_cutoff(principal, user_row.sessions_valid_after):
            raise unauthenticated()
        if sp_row is not None and issued_before_cutoff(principal, sp_row.sessions_valid_after):
            raise unauthenticated()
        return _EntraAdmission(entry, group, user_row, sp_row)

    async def _resolve_pat(self, principal: Principal) -> ResolvedPrincipal:
        """Load the credential live and re-prove its owner on every request (D-65, D-66, D-72)."""
        if principal.credential_id is None:
            raise unauthenticated()
        async with self._session_factory() as session:
            found = (
                await session.execute(
                    select(ApiCredential, User, EntraUser)
                    .select_from(ApiCredential)
                    .outerjoin(User, User.id == ApiCredential.local_user_id)
                    .outerjoin(EntraUser, EntraUser.id == ApiCredential.entra_user_id)
                    .where(
                        ApiCredential.public_id == principal.credential_id,
                        ApiCredential.kind == "pat",
                        ApiCredential.revoked_at.is_(None),
                        ApiCredential.expires_at > func.now(),
                    )
                )
            ).one_or_none()
        if found is None:
            raise unauthenticated()
        credential, local_user, entra_user = found
        resolved: ResolvedPrincipal
        if local_user is not None:
            if local_principal_id(local_user.id) != principal.id or not local_user.is_active:
                raise unauthenticated()
            resolved = ResolvedPrincipal("user", principal.id, read_only=credential.read_only)
        elif entra_user is not None:
            owner_id = entra_principal_id(entra_user.tenant_id, entra_user.object_id)
            if owner_id != principal.id or not entra_user.is_active:
                raise unauthenticated()
            admitted = await self._admit_entra(
                principal, "user", entra_user.tenant_id, entra_user.object_id
            )
            resolved = ResolvedPrincipal(
                "user",
                principal.id,
                tuple(sorted(admitted.entry.group_ids)),
                read_only=credential.read_only,
            )
        else:
            raise unauthenticated()
        # Memory only: the batched flush writes last_used_at, never this request (T-04-51).
        if self._usage is not None:
            self._usage.record(credential.id, datetime.now(UTC))
        return resolved

    async def _resolve_api_key(self, principal: Principal) -> ResolvedPrincipal:
        """Load the key live and re-prove its service account on every request (T-04-30)."""
        if principal.credential_id is None:
            raise unauthenticated()
        async with self._session_factory() as session:
            found = (
                await session.execute(
                    select(ApiCredential, ServiceAccount)
                    .select_from(ApiCredential)
                    .join(ServiceAccount, ServiceAccount.id == ApiCredential.service_account_id)
                    .where(
                        ApiCredential.public_id == principal.credential_id,
                        ApiCredential.kind == "service_account_key",
                        ApiCredential.revoked_at.is_(None),
                        ApiCredential.expires_at > func.now(),
                    )
                )
            ).one_or_none()
        if found is None:
            raise unauthenticated()
        credential, account = found
        if service_account_principal_id(account.id) != principal.id or not account.is_active:
            raise unauthenticated()
        # Memory only: the batched flush writes last_used_at, never this request (T-04-51).
        if self._usage is not None:
            self._usage.record(credential.id, datetime.now(UTC))
        return ResolvedPrincipal("service_account", principal.id)

    @staticmethod
    async def _directly_registered(
        session: AsyncSession, kind: str, tenant_id: str, object_id: str
    ) -> bool:
        found = await session.scalar(
            select(EntraRegistration.id).where(
                EntraRegistration.kind == kind,
                EntraRegistration.tenant_id == tenant_id,
                EntraRegistration.object_id == object_id,
                EntraRegistration.is_active.is_(True),
                EntraRegistration.directly_registered.is_(True),
            )
        )
        return found is not None

    @staticmethod
    async def _admitting_group(
        session: AsyncSession, tenant_id: str, group_ids: frozenset[str]
    ) -> EntraRegistration | None:
        """The oldest (smallest uuidv7 id) active registered group of the tenant that admits."""
        if not group_ids:
            return None
        return await session.scalar(
            select(EntraRegistration)
            .where(
                EntraRegistration.kind == "group",
                EntraRegistration.tenant_id == tenant_id,
                EntraRegistration.object_id.in_(sorted(group_ids)),
                EntraRegistration.is_active.is_(True),
            )
            .order_by(EntraRegistration.id)
            .limit(1)
        )

    @staticmethod
    async def _user_row(session: AsyncSession, tenant_id: str, object_id: str) -> EntraUser | None:
        return await session.scalar(
            select(EntraUser).where(
                EntraUser.tenant_id == tenant_id, EntraUser.object_id == object_id
            )
        )

    @staticmethod
    async def _service_principal_row(
        session: AsyncSession, tenant_id: str, object_id: str
    ) -> EntraRegistration | None:
        """The service principal's row, direct or just-in-time (never a group's)."""
        return await session.scalar(
            select(EntraRegistration).where(
                EntraRegistration.kind == "service_principal",
                EntraRegistration.tenant_id == tenant_id,
                EntraRegistration.object_id == object_id,
            )
        )

    async def _record(
        self,
        principal: Principal,
        tenant_id: str,
        object_id: str,
        entry: DirectoryEntry,
        user_row: EntraUser | None,
        sp_row: EntraRegistration | None,
        group: EntraRegistration | None,
    ) -> None:
        """Just-in-time writes after admission, outside the read session; never for a refusal."""
        # Imported here, not at module level: ``entra`` also hosts the registration services,
        # which use the guards and the PDP, and those import this module.
        from dc_identity.entra import ensure_entra_service_principal, ensure_entra_user

        if principal.type == "user" and principal.via == "jwt":
            display = (principal.display or object_id)[:200]
            hint = principal.username_hint[:256] if principal.username_hint else None
            if (
                user_row is None
                or user_row.display_name != display
                or (hint is not None and user_row.preferred_username != hint)
            ):
                await ensure_entra_user(
                    self._session_factory,
                    principal,
                    tenant_id=tenant_id,
                    object_id=object_id,
                    service=self._service,
                )
        elif (
            principal.type == "service_principal"
            and sp_row is None
            and group is not None
            and group.owner_folder_id is not None
        ):
            await ensure_entra_service_principal(
                self._session_factory,
                principal,
                tenant_id=tenant_id,
                object_id=object_id,
                owner_folder_id=group.owner_folder_id,
                display_name=entry.display_name or principal.display,
                service=self._service,
            )
