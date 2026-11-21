"""The security settings in force, and the sign-in lockout rules (quick 261120-0ye, D-37 to D-41).

Identity owns the stored overrides (section ``security``); the ``IdentitySettings`` env values are
the defaults. ``effective_policy`` merges the two once per request (the store has a 5 s cache), so
every enforcement point (login, refresh, the gate, throttling, credentials, password rules) reads
one frozen object.

Sign-in methods (S1): the effective local method is on unless the stored setting turns it off, and
``DC_SIGNIN_LOCAL_FORCE_ENABLED`` forces it on again (the documented break-glass recovery). The
Microsoft method is on only when an Entra issuer is configured (a capability) and the stored
setting does not turn it off; a stored setting can switch a method off, never on without its
capability.

``check_signin_change`` is the one function that decides whether a proposed ``security`` section
may be saved (``PUT`` and import call it): it refuses every change that could leave nobody able to
sign in, and the actor's own method can never be switched off from under them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dc_auth.principal import Principal
from dc_core.problems import ProblemError
from sqlalchemy import text

from dc_identity.password_policy import PasswordRules
from dc_identity.principal_ids import parse_principal_id

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from dc_identity.platform_settings import SettingsStore
    from dc_identity.settings import IdentitySettings

SLUG = "signin-lockout-risk"


@dataclass(frozen=True)
class SecurityPolicy:
    """Everything the ``security`` section controls, with the env defaults filled in."""

    local_signin: bool
    microsoft_signin: bool
    credential_max_ttl_days: int
    max_active_tokens: int
    max_sessions_per_user: int
    session_max_s: int
    idle_timeout_s: int
    lockout_account_threshold: int
    lockout_ip_threshold: int
    lockout_max_lock_s: int
    password_rules: PasswordRules


def env_defaults(settings: IdentitySettings) -> dict[str, Any]:
    """The env values of every ``security`` field, in the shape of the section (the view's
    defaults)."""
    return {
        "local_signin_enabled": True,
        "microsoft_signin_enabled": True,
        "credential_max_ttl_days": settings.credential_max_ttl_days,
        "max_active_tokens": settings.max_active_tokens,
        "max_sessions_per_user": settings.max_sessions_per_user,
        "session_max_s": settings.session_max_s,
        "idle_timeout_s": min(settings.refresh_token_ttl_s, settings.session_max_s),
        "lockout_account_threshold": settings.throttle_account_threshold,
        "lockout_ip_threshold": settings.throttle_ip_threshold,
        "lockout_max_lock_s": settings.throttle_max_lock_s,
        "password_min_length": PasswordRules.default().min_length,
        "password_require_upper": False,
        "password_require_lower": False,
        "password_require_digit": False,
        "password_require_symbol": False,
    }


def effective_policy(
    stored: Mapping[str, Any], settings: IdentitySettings, *, entra_configured: bool
) -> SecurityPolicy:
    """Stored overrides over the env defaults."""
    base = env_defaults(settings)

    def pick(name: str) -> Any:
        value = stored.get(name)
        return base[name] if value is None else value

    session_max = int(pick("session_max_s"))
    # An idle timeout above the session length is meaningless: the session cap wins.
    idle = min(int(pick("idle_timeout_s")), session_max)
    local = stored.get("local_signin_enabled") is not False or settings.signin_local_force_enabled
    microsoft = entra_configured and stored.get("microsoft_signin_enabled") is not False
    return SecurityPolicy(
        local_signin=local,
        microsoft_signin=microsoft,
        credential_max_ttl_days=int(pick("credential_max_ttl_days")),
        max_active_tokens=int(pick("max_active_tokens")),
        max_sessions_per_user=int(pick("max_sessions_per_user")),
        session_max_s=session_max,
        idle_timeout_s=idle,
        lockout_account_threshold=int(pick("lockout_account_threshold")),
        lockout_ip_threshold=int(pick("lockout_ip_threshold")),
        lockout_max_lock_s=int(pick("lockout_max_lock_s")),
        password_rules=PasswordRules(
            min_length=int(pick("password_min_length")),
            require_upper=bool(pick("password_require_upper")),
            require_lower=bool(pick("password_require_lower")),
            require_digit=bool(pick("password_require_digit")),
            require_symbol=bool(pick("password_require_symbol")),
        ),
    )


async def load_policy(
    store: SettingsStore, settings: IdentitySettings, *, entra_configured: bool
) -> SecurityPolicy:
    return effective_policy(
        (await store.section("security")).value, settings, entra_configured=entra_configured
    )


async def request_policy(request: Request) -> SecurityPolicy:
    """The policy in force for this request (stored overrides over the env defaults)."""
    state = request.app.state
    return await load_policy(
        state.platform_settings, state.settings, entra_configured=bool(state.entra_tenant_ids)
    )


# --- the lockout rules (D-37) --------------------------------------------------------------------

_LOCAL_ADMIN = text(
    """
    SELECT EXISTS (
      SELECT 1 FROM role_assignments ra
      JOIN folder f ON f.id = ra.folder_id AND f.kind = 'root'
      WHERE ra.role = 'global_admin' AND (
        (ra.principal_type = 'user' AND EXISTS (
          SELECT 1 FROM users u
          WHERE u.is_active AND ra.principal_id = 'local:' || CAST(u.id AS text)))
        OR (ra.principal_type = 'local_group' AND EXISTS (
          SELECT 1 FROM group_members gm
          JOIN users u ON gm.member_type = 'user'
                      AND gm.member_id = 'local:' || CAST(u.id AS text)
          WHERE CAST(gm.group_id AS text) = ra.principal_id AND u.is_active))
      )
    )
    """
)

_ENTRA_ADMIN = text(
    """
    SELECT EXISTS (
      SELECT 1 FROM role_assignments ra
      JOIN folder f ON f.id = ra.folder_id AND f.kind = 'root'
      WHERE ra.role = 'global_admin' AND (
        (ra.principal_type = 'user' AND EXISTS (
          SELECT 1 FROM entra_registrations er
          WHERE er.kind = 'user' AND er.is_active AND er.directly_registered
            AND ra.principal_id = 'entra:' || er.tenant_id || ':' || er.object_id))
        OR (ra.principal_type = 'entra_group' AND EXISTS (
          SELECT 1 FROM entra_registrations er
          WHERE er.kind = 'group' AND er.is_active AND er.object_id = ra.principal_id))
      )
    )
    """
)


def actor_method(actor: Principal) -> str | None:
    """The sign-in method an interactive admin is using: ``local``, ``microsoft`` or ``None``."""
    ref = parse_principal_id(actor.id)
    if ref is None or actor.via != "jwt":
        return None
    if ref.kind == "local_user":
        return "local"
    if ref.kind == "entra" and actor.type == "user":
        return "microsoft"
    return None


def _refuse(status: int, detail: str, pointer: str) -> ProblemError:
    return ProblemError(
        status,
        SLUG,
        "Unprocessable Content" if status == 422 else "Conflict",
        detail,
        errors=[{"pointer": pointer, "detail": detail}],
    )


def _on(section: Mapping[str, Any], name: str) -> bool:
    return section.get(name) is not False


async def check_signin_change(
    session: AsyncSession,
    *,
    actor: Principal,
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
    entra_configured: bool,
    pointer_base: str = "",
) -> None:
    """Refuse a ``security`` section that could lock everyone out.

    (a) both methods off: 422. (b) switching off the method the acting admin is signed in with:
    409. (c) newly switching local off without a configured Entra issuer and an active Entra
    Global admin: 409. (d) newly switching Microsoft off without an active local Global admin:
    409. (b) forces the actor onto the remaining method, which proves it works right now. Rules
    (b) to (d) apply to a *change*: a section that keeps an already-off method off (for example
    while the force-enable recovery is in use) can still be saved.
    """
    local, microsoft = (
        _on(proposed, "local_signin_enabled"),
        _on(proposed, "microsoft_signin_enabled"),
    )
    if not local and not microsoft:
        raise _refuse(
            422,
            "Local and Microsoft sign-in cannot both be turned off.",
            f"{pointer_base}/local_signin_enabled",
        )
    turning_off_local = not local and _on(current, "local_signin_enabled")
    turning_off_microsoft = not microsoft and _on(current, "microsoft_signin_enabled")
    method = actor_method(actor)
    if method == "local" and turning_off_local:
        raise _refuse(
            409,
            "You are signed in with a local account; turn local sign-in off from a "
            "Microsoft session instead.",
            f"{pointer_base}/local_signin_enabled",
        )
    if method == "microsoft" and turning_off_microsoft:
        raise _refuse(
            409,
            "You are signed in with Microsoft; turn Microsoft sign-in off from a local "
            "session instead.",
            f"{pointer_base}/microsoft_signin_enabled",
        )
    if turning_off_local:
        has_entra_admin = entra_configured and bool(
            (await session.execute(_ENTRA_ADMIN)).scalar_one()
        )
        if not has_entra_admin:
            raise _refuse(
                409,
                "Local sign-in can only be turned off while Microsoft sign-in is configured "
                "and an active Microsoft Global admin exists.",
                f"{pointer_base}/local_signin_enabled",
            )
    if turning_off_microsoft and not bool((await session.execute(_LOCAL_ADMIN)).scalar_one()):
        raise _refuse(
            409,
            "Microsoft sign-in can only be turned off while an active local Global admin exists.",
            f"{pointer_base}/microsoft_signin_enabled",
        )


__all__ = [
    "SLUG",
    "SecurityPolicy",
    "actor_method",
    "check_signin_change",
    "effective_policy",
    "env_defaults",
    "load_policy",
    "request_policy",
]
