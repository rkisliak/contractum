"""``send_email``: a message the mail service would refuse is never sent; a timeout is a code."""

from __future__ import annotations

import httpx
import pytest
from dc_contracts.mailer import EmailOutcome, send_email
from dc_contracts.settings import ContractsSettings
from pydantic import SecretStr

pytestmark = pytest.mark.unit

KEY = "dcp_testkey_notifier-secret-value"  # noqa: S105 - a test token, not a secret


def _settings() -> ContractsSettings:
    return ContractsSettings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
        mail_url="http://mail:8000",
        mail_api_key=SecretStr(KEY),
    )


async def _send(handler: httpx.MockTransport, recipients: list[str]) -> EmailOutcome:
    async with httpx.AsyncClient(transport=handler) as client:
        return await send_email(client, _settings(), recipients, "subject", "text")


async def test_a_recipient_the_mail_service_would_refuse_is_never_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"refused": []})

    outcome = await _send(httpx.MockTransport(handler), ["not an address"])
    assert outcome == EmailOutcome(False, 0, "mail-invalid-message")
    assert seen == []


async def test_a_mail_service_that_does_not_answer_in_time_is_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    outcome = await _send(httpx.MockTransport(handler), ["a@example.test"])
    assert outcome == EmailOutcome(False, 0, "mail-timeout")
