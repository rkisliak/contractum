"""``POST /api/v1/contracts/settings/test-email`` (quick 261120-0yd, N3, D-27).

A Global admin sends one fixed test message to an address of their choice, through the same
``send_email`` path and mail service the delivery worker uses, but with the **caller's own**
bearer (a Global admin holds ``mail:send``). The route is ``settings:write`` (a ROOT permission
held by the Global admin only, interactive sign-in only: a PAT or key is refused by the PDP) and
rate limited in process: 5 per 10 minutes per principal and 30 per hour per process, else 429
``too-many-attempts`` with ``Retry-After``.

Every attempt that reaches the send step writes one ``settings.test_email_sent`` event in
contracts' outbox (aggregate ``platform_settings`` / ``notifications``, empty scope, so it is
visible at ROOT only) holding the recipient's **domain**, the outcome and the error code. The full
address is never stored, logged or audited.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Annotated, Any, Literal

import structlog
from dc_auth.deps import UnitOfWorkDep, authenticate, parse_bearer, require
from dc_auth.principal import Principal
from dc_core.body import read_body_capped
from dc_core.platform_settings import check_email_address
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from dc_contracts.mailer import EmailOutcome, send_email

_log = structlog.get_logger("dc_contracts.test_email")

router = PolicyRouter(prefix="/api/v1/contracts/settings")

BODY_MAX_BYTES = 1024
PER_PRINCIPAL_LIMIT, PER_PRINCIPAL_WINDOW_S = 5, 600.0
PER_PROCESS_LIMIT, PER_PROCESS_WINDOW_S = 30, 3600.0
TEST_SUBJECT = "[Contractum] Test email"
TEST_BODY = (
    "This is a test message from Contractum. The sender and the templates come from "
    "Admin > Settings > Notifications.\n"
)


class ContractsTestEmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str

    @field_validator("to")
    @classmethod
    def _address(cls, value: str) -> str:
        return check_email_address(value)


class ContractsTestEmailOut(BaseModel):
    outcome: Literal["sent", "refused", "unavailable", "not-configured"]
    error_code: str | None


class TestEmailLimiter:
    """Sliding windows, in process: a per-principal one and a per-process one (T-0yd-06)."""

    __test__ = False  # not a pytest class

    def __init__(self) -> None:
        self._principals: dict[str, deque[float]] = {}
        self._process: deque[float] = deque()

    @staticmethod
    def _trim(hits: deque[float], now: float, window: float) -> None:
        while hits and now - hits[0] >= window:
            hits.popleft()

    def check(self, principal_id: str) -> int | None:
        """``None`` when allowed (and counted), else the seconds until a slot frees up."""
        now = time.monotonic()
        own = self._principals.setdefault(principal_id, deque())
        self._trim(own, now, PER_PRINCIPAL_WINDOW_S)
        self._trim(self._process, now, PER_PROCESS_WINDOW_S)
        waits: list[int] = []
        if len(own) >= PER_PRINCIPAL_LIMIT:
            waits.append(int(PER_PRINCIPAL_WINDOW_S - (now - own[0])) + 1)
        if len(self._process) >= PER_PROCESS_LIMIT:
            waits.append(int(PER_PROCESS_WINDOW_S - (now - self._process[0])) + 1)
        if waits:
            return max(1, max(waits))
        own.append(now)
        self._process.append(now)
        if len(self._principals) > 1000:  # forget principals with nothing in the window
            # Only the caller's own window is trimmed above, so trim each one before judging it:
            # an idle principal keeps stale entries until then and would never be forgotten.
            for key, hits in list(self._principals.items()):
                self._trim(hits, now, PER_PRINCIPAL_WINDOW_S)
                if not hits:
                    del self._principals[key]
        return None


def _outcome(result: EmailOutcome) -> Literal["sent", "refused", "unavailable", "not-configured"]:
    if result.ok:
        return "sent"
    if result.error == "mail-not-configured":
        return "not-configured"
    if result.error in ("mail-recipients-refused", "mail-invalid-message"):
        return "refused"
    if result.status is not None and 400 <= result.status < 500:
        return "refused"
    return "unavailable"


def _invalid(pointer: str, detail: str) -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Unprocessable Content",
        detail,
        errors=[{"pointer": pointer, "detail": detail}],
    )


async def _parse(request: Request) -> ContractsTestEmailIn:
    raw = await read_body_capped(request, BODY_MAX_BYTES, detail="The request body is too large.")
    try:
        return ContractsTestEmailIn.model_validate_json(raw)
    except ValidationError as exc:
        first = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        pointer = "/" + "/".join(str(p) for p in first["loc"]) if first["loc"] else ""
        if first["type"] == "extra_forbidden":
            pointer = ""  # the key name is caller input
        message = (
            "Enter a plain email address such as name@example.com."
            if pointer == "/to"
            else ("The request body is not valid.")
        )
        raise _invalid(pointer, message) from None


@router.post(
    "/test-email",
    response_model=ContractsTestEmailOut,
    dependencies=[require("settings:write")],
    summary="Send a test email through the mail service",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": ContractsTestEmailIn.model_json_schema()}},
        },
    },
    responses={
        401: {"description": "No valid token"},
        403: {
            "description": (
                "Only a Global admin may send a test email, and only after an interactive "
                "sign-in (interactive-session-required for a PAT or key)"
            )
        },
        413: {"description": "The request body is larger than 1 KiB"},
        422: {"description": "The address is not acceptable"},
        429: {"description": "At most 5 test emails per 10 minutes per user (Retry-After)"},
    },
)
async def send_test_email(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> ContractsTestEmailOut:
    body = await _parse(request)
    wait = request.app.state.test_email_limiter.check(principal.id)
    if wait is not None:
        raise ProblemError(
            429,
            "too-many-attempts",
            "Too Many Requests",
            "Too many test emails; try again later.",
            headers={"Retry-After": str(wait)},
            retry_after_s=wait,
        )
    settings = request.app.state.settings
    result = await send_email(
        request.app.state.outbound_http,
        settings,
        [body.to],
        TEST_SUBJECT,
        TEST_BODY,
        bearer=parse_bearer(request),
    )
    outcome = _outcome(result)
    domain = body.to.rpartition("@")[2].lower()
    after: dict[str, Any] = {
        "recipient_domain": domain,
        "outcome": outcome,
        "error_code": result.error,
    }
    uow.audit(
        "settings.test_email_sent",
        "platform_settings",
        "notifications",
        before=None,
        after=after,
        scope_path=(),
    )
    await uow.commit()
    # Counts and codes only: never the address.
    _log.info("settings_test_email", principal_id=principal.id, outcome=outcome, code=result.error)
    return ContractsTestEmailOut(outcome=outcome, error_code=result.error)


__all__ = ["TestEmailLimiter", "router"]
