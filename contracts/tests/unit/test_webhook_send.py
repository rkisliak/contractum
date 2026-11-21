"""``send_webhook``: the response body is never read and the response is closed (quick 261121-0sh).

The transport is an ``httpx.MockTransport`` whose response stream records whether it was iterated
and whether it was closed.
"""

from __future__ import annotations

import ssl
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from dc_contracts import webhooks

pytestmark = pytest.mark.unit

TARGET = webhooks.ParsedWebhookUrl(
    scheme="https", host="hooks.example.test", port=443, path_qs="/h"
)


@dataclass(frozen=True)
class _Settings:
    webhook_timeout_s: float = 5.0


class _RecordingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.iterated = False
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.iterated = True
        yield b"response body that must never be read"

    async def aclose(self) -> None:
        self.closed = True


async def _send(status: int) -> tuple[webhooks.DeliveryOutcome, _RecordingStream]:
    stream = _RecordingStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await webhooks.send_webhook(
            client, TARGET, ["whsec_test"], uuid.uuid4(), b"{}", _Settings()
        )
    return outcome, stream


async def test_a_2xx_is_delivered_and_the_body_is_never_read_but_closed() -> None:
    outcome, stream = await _send(204)
    assert outcome == webhooks.DeliveryOutcome(True, 204, None)
    assert stream.closed is True
    assert stream.iterated is False


@pytest.mark.parametrize("status", [301, 404, 500])
async def test_a_non_2xx_is_an_http_code_and_the_body_is_never_read(status: int) -> None:
    outcome, stream = await _send(status)
    assert outcome == webhooks.DeliveryOutcome(False, status, f"http-{status}")
    assert stream.closed is True
    assert stream.iterated is False


async def _send_raising(error: Exception) -> webhooks.DeliveryOutcome:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await webhooks.send_webhook(
            client, TARGET, ["whsec_test"], uuid.uuid4(), b"{}", _Settings()
        )


async def test_a_receiver_that_does_not_answer_in_time_is_a_timeout() -> None:
    outcome = await _send_raising(httpx.ReadTimeout("slow"))
    assert outcome == webhooks.DeliveryOutcome(False, None, "timeout")


async def test_a_url_httpx_cannot_send_to_is_an_invalid_url() -> None:
    outcome = await _send_raising(httpx.InvalidURL("bad"))
    assert outcome == webhooks.DeliveryOutcome(False, None, "invalid-url")


async def test_a_failed_tls_handshake_is_a_tls_error_not_a_connect_error() -> None:
    try:
        raise httpx.ConnectError("handshake") from ssl.SSLError("certificate verify failed")
    except httpx.ConnectError as error:
        outcome = await _send_raising(error)
    assert outcome == webhooks.DeliveryOutcome(False, None, "tls-error")


async def test_a_refused_connection_is_a_connect_error() -> None:
    outcome = await _send_raising(httpx.ConnectError("refused"))
    assert outcome == webhooks.DeliveryOutcome(False, None, "connect-error")
