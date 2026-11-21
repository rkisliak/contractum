"""User domain service: a Global admin creates local users who start with no access (RBAC-10).

Decision D-26: a new user holds no role assignment, so every folder list is an empty page and every
folder addressed by id is 404 until a grant is made. Decision OQ1: admin-created users are not
forced to rotate their password at first login in Phase 2 (deferred).

The password follows the Phase 1 policy and is stored only as an Argon2id hash. The audit snapshot
comes from ``User.__audit_fields__``, which carries the derived ``password_set`` and ``email_set``
flags and never the password, its hash or the address.
"""

from __future__ import annotations

import re
import uuid

from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy.exc import IntegrityError

from dc_identity.deactivation import _locked_user
from dc_identity.models import User
from dc_identity.password_policy import PasswordRules, check_password
from dc_identity.passwords import PasswordService
from dc_identity.sessions import root_folder_id

# 3-64 characters of a-z, 0-9, ".", "_" and "-", starting and ending alphanumeric.
USERNAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,62}[a-z0-9]$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+$"
EMAIL_MAX_LENGTH = 254

_USERNAME_RE = re.compile(USERNAME_PATTERN)
_DUPLICATE_CONSTRAINTS = frozenset({"uq_users_username"})


def normalize_username(username: str) -> str:
    return username.strip().lower()


def _invalid(pointer: str, detail: str) -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Unprocessable Content",
        "The request failed validation.",
        errors=[{"pointer": pointer, "detail": detail}],
    )


async def create_user(
    uow: UnitOfWork,
    passwords: PasswordService,
    *,
    username: str,
    password: str,
    display_name: str,
    email: str | None,
    rules: PasswordRules | None = None,
) -> User:
    """Create an active local user with a hashed password and no access; one ``user.created``.

    ``rules`` are the effective password rules (quick 261120-0ye, D-41); a new password only.
    """
    username = normalize_username(username)
    if _USERNAME_RE.fullmatch(username) is None:
        raise _invalid(
            "/username",
            "username must be 3 to 64 characters of a-z, 0-9, '.', '_' or '-', "
            "starting and ending with a letter or digit",
        )
    check_password(password, username=username, rules=rules)
    password_hash = await passwords.hash(password)

    session = uow.session
    user = User(
        username=username,
        display_name=display_name,
        password_hash=password_hash,
        email=email,
        must_rotate=False,
        is_active=True,
    )
    # No SAVEPOINT (see create_folder): a failed flush aborts the whole unit of work, and the 409
    # below makes UnitOfWork roll back. The unique constraint, not a pre-check, is the arbiter, so
    # two concurrent creates of the same username yield exactly one 201.
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint in _DUPLICATE_CONSTRAINTS:
            raise ProblemError(
                409, "conflict", "Conflict", "A user with this username already exists."
            ) from exc
        raise
    await session.refresh(user)
    uow.audit(
        "user.created",
        "user",
        user.id,
        before=None,
        after=UnitOfWork.snapshot(user),
        scope_path=[await root_folder_id(session)],
    )
    await uow.commit()
    return user


async def change_display_name(uow: UnitOfWork, user_id: uuid.UUID, display_name: str) -> User:
    """Set a local user's display name; one ``user.display_name_changed`` per effective change.

    The row is locked, so two concurrent renames serialise and each audits the name it replaced.
    The current name again is returned unchanged with no write and no event. A name is not
    authority, so this does not take the authz write lock. Unknown id: 404 ``not-found``.
    """
    session = uow.session
    user = await _locked_user(session, user_id)
    if user.display_name == display_name:
        return user
    before = uow.snapshot(user)
    user.display_name = display_name
    await session.flush()
    await session.refresh(user)
    uow.audit(
        "user.display_name_changed",
        "user",
        user.id,
        before=before,
        after=uow.snapshot(user),
        scope_path=[await root_folder_id(session)],
    )
    await uow.commit()
    return user


__all__ = [
    "EMAIL_MAX_LENGTH",
    "EMAIL_PATTERN",
    "USERNAME_PATTERN",
    "change_display_name",
    "create_user",
    "normalize_username",
]
