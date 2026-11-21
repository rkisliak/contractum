"""DenialRecorder: one identity outbox row per denied permission check (AUD-04, D-172, D-173).

``LocalAuthorizer.check`` calls :meth:`DenialRecorder.record` for every outcome that is not ALLOW
(NOT_FOUND, FORBIDDEN, a gate refusal and the D-79 interactive-only refusal) after the decision's
own session has closed, so a check never holds two pooled connections. The row is written in its
own short transaction and has committed before the decision is returned or the refusal raised.
The existing outbox relay, the ``AUDIT`` stream and audit's idempotent ingest deliver it as
``audit.identity.authz.check.denied`` with category ``denial``.

A row that cannot be written is never swallowed: the check answers 503 ``service-unavailable``
(still a refusal, fail closed), so identity never returns an unrecorded denial.

Content (D-173): ``aggregate_id`` is ``root`` for a root permission, else the target folder id,
else ``none``. ``scope_path`` is the target folder's ancestors when it exists, ``[ROOT]`` for a
root permission and empty otherwise (only ROOT auditors see probes of unknown ids). ``detail`` has
exactly the keys of :data:`DETAIL_KEYS`; only the route template is recorded, never a raw URL or
query string. ``before`` and ``after`` are always null.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, Literal

import structlog
import uuid_utils.compat as uuid_compat
from asgi_correlation_id import correlation_id
from dc_auth.authz_wire import CheckContext
from dc_auth.principal import Principal, principal_to_actor
from dc_auth.roles import PERMISSIONS
from dc_core.problems import ProblemError

from dc_identity.access import root_id
from dc_identity.models import OutboxEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger("dc_identity.denials")

AGGREGATE_TYPE: Final = "authz"
ACTION: Final = "check.denied"
DETAIL_KEYS: Final = frozenset({"permission", "outcome", "service", "route"})
OUTCOMES: Final = frozenset({"not_found", "forbidden", "gate_refused", "interactive_required"})
MAX_PERMISSION_LENGTH: Final = 64
DEFAULT_CONTEXT: Final = CheckContext(service="identity", route="unknown")

DenialOutcome = Literal["not_found", "forbidden", "gate_refused", "interactive_required"]


def is_root_permission(permission: str) -> bool:
    spec = PERMISSIONS.get(permission)
    return spec is not None and spec.target == "root"


def aggregate_id_for(permission: str, folder_id: uuid.UUID | None) -> str:
    if is_root_permission(permission):
        return "root"
    return str(folder_id) if folder_id is not None else "none"


class DenialRecorder:
    """Writes the denial row; raises ``ProblemError(503)`` when it cannot (fail closed)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        principal: Principal,
        permission: str,
        outcome: DenialOutcome,
        *,
        folder_id: uuid.UUID | None,
        scope_path: Sequence[uuid.UUID] | None,
        context: CheckContext,
    ) -> None:
        """Commit one ``check.denied`` row before returning.

        ``scope_path=None`` means the decision did not establish it (a gate refusal, or a branch
        that never looked at the target): it is then resolved here, in this session.
        """
        if outcome not in OUTCOMES:
            raise ValueError(f"not a denial outcome: {outcome!r}")
        detail = {
            "permission": permission[:MAX_PERMISSION_LENGTH],
            "outcome": outcome,
            "service": context.service,
            "route": context.route,
        }
        try:
            async with self._session_factory() as session, session.begin():
                if scope_path is None:
                    scope_path = await self._scope_path(session, permission, folder_id)
                session.add(
                    OutboxEvent(
                        event_id=uuid_compat.uuid7(),
                        aggregate_type=AGGREGATE_TYPE,
                        aggregate_id=aggregate_id_for(permission, folder_id),
                        aggregate_version=None,
                        action=ACTION,
                        scope_path=list(scope_path),
                        actor=principal_to_actor(principal).model_dump(),
                        request_id=correlation_id.get(),
                        before=None,
                        after=None,
                        detail=detail,
                    )
                )
        except Exception as exc:
            _log.error("denial_not_recorded", error=type(exc).__name__)
            raise ProblemError(
                503,
                "service-unavailable",
                "Service Unavailable",
                "Authorization is temporarily unavailable.",
            ) from exc

    @staticmethod
    async def _scope_path(
        session: AsyncSession, permission: str, folder_id: uuid.UUID | None
    ) -> Sequence[uuid.UUID]:
        if is_root_permission(permission):
            return [await root_id(session)]
        if folder_id is None:
            return []
        # Imported here: authz imports this module for the recorder.
        from dc_identity.authz import load_folder_facts

        facts = await load_folder_facts(session, folder_id)
        return list(facts.ancestor_ids) if facts is not None else []
