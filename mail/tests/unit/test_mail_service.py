"""The mail service against an in-process SMTP sink (D-105, SUB-07)."""

from __future__ import annotations

import asyncio
import email
import email.policy
import smtplib
import ssl
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
from dc_auth.authorizer import Decision, FolderScope, Outcome
from dc_auth.issuers import AuthSettings
from dc_auth.principal import Principal
from dc_auth.testing import FakeIssuer
from dc_mail.main import create_mail_app
from dc_mail.settings import MailSettings, is_internal_smtp_host
from pydantic import ValidationError
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

SEND = "/internal/v1/mail/send"
NOTIFIER = "fake:notifier"


@dataclass
class SmtpSink:
    """A tiny SMTP server: records envelopes and DATA, refuses chosen recipients."""

    port: int = 0
    refuse: set[str] = field(default_factory=set)
    defer: set[str] = field(default_factory=set)
    mail_from_reply: str = "250 ok"
    messages: list[tuple[str, list[str], bytes]] = field(default_factory=list)
    _loop: asyncio.AbstractEventLoop | None = None
    _thread: threading.Thread | None = None
    _server: asyncio.Server | None = None

    def start(self) -> None:
        """Serve from a thread with its own loop, so the sink works under any test loop scope."""
        loop = asyncio.new_event_loop()
        self._server = loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
        self.port = self._server.sockets[0].getsockname()[1]
        self._loop = loop
        self._thread = threading.Thread(target=loop.run_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop is None or self._thread is None or self._server is None:
            return
        server = self._server

        async def _close() -> None:
            server.close()
            await server.wait_closed()

        asyncio.run_coroutine_threadsafe(_close(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()
        self._loop = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def say(line: str) -> None:
            writer.write(line.encode() + b"\r\n")
            await writer.drain()

        sender = ""
        rcpts: list[str] = []
        try:
            await say("220 sink ESMTP")
            while line := (await reader.readline()).decode().rstrip("\r\n"):
                verb = line.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    await say("250 sink")
                elif verb == "MAIL":
                    if not self.mail_from_reply.startswith("250"):
                        await say(self.mail_from_reply)
                        continue
                    sender = line.partition(":")[2].strip().strip("<>")
                    rcpts = []
                    await say("250 ok")
                elif verb == "RCPT":
                    addr = line.partition(":")[2].strip().strip("<>")
                    if addr in self.refuse:
                        await say("550 no such user")
                    elif addr in self.defer:
                        await say("450 greylisted, try later")
                    else:
                        rcpts.append(addr)
                        await say("250 ok")
                elif verb == "DATA":
                    await say("354 go")
                    data = b""
                    while (chunk := await reader.readline()) != b".\r\n":
                        data += chunk[1:] if chunk.startswith(b"..") else chunk  # un-stuff
                    self.messages.append((sender, rcpts, data))
                    await say("250 queued")
                elif verb == "QUIT":
                    await say("221 bye")
                    break
                else:
                    await say("250 ok")
        finally:
            writer.close()

    def parsed(self, index: int = 0) -> EmailMessage:
        msg = email.message_from_bytes(self.messages[index][2], policy=email.policy.default)
        assert isinstance(msg, EmailMessage)
        return msg


@pytest.fixture
def sink() -> Iterator[SmtpSink]:
    s = SmtpSink()
    s.start()
    yield s
    s.stop()


class StubAuthorizer:
    """ALLOW mail:send for the notifier only (what identity's PDP answers, Plan 04)."""

    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: Any = None,
        *,
        fresh: bool = False,
    ) -> Decision:
        allowed = principal.id == NOTIFIER and permission == "mail:send"
        return Decision(outcome=Outcome.ALLOW if allowed else Outcome.FORBIDDEN)

    async def accessible_folders(self, principal: Principal, permission: str) -> FolderScope:
        return FolderScope(all=False)


@pytest.fixture
def make_app(fake_issuer: FakeIssuer) -> Callable[..., Any]:
    def _make(
        sink: SmtpSink | None, *, http_client: httpx.AsyncClient | None = None, **overrides: Any
    ) -> Any:
        params: dict[str, Any] = {
            "smtp_host": "127.0.0.1",
            "smtp_port": sink.port if sink else 25,
            "smtp_tls": "none",
            "smtp_from": "dc@example.test",
        }
        params.update(overrides)
        return create_mail_app(
            MailSettings(**params),
            AuthSettings(issuers=[fake_issuer.issuer_config()]),
            http_client=http_client,
            authorizer_factory=lambda request: StubAuthorizer(),
        )

    return _make


def _auth(fake_issuer: FakeIssuer, sub: str = "notifier") -> dict[str, str]:
    return {"Authorization": f"Bearer {fake_issuer.mint(sub, ptype='service_account')}"}


BODY = {
    "to": ["a@example.test", "b@example.test"],
    "subject": "orders 2.3.0 deprecated",
    "text": "Line 1\nLine 2",
}


async def test_an_authorized_caller_posts_a_message_and_it_reaches_smtp_from_the_fixed_sender(
    sink: SmtpSink,
    make_app: Callable[..., Any],
    fake_issuer: FakeIssuer,
    asgi_client: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]],
) -> None:
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 2, "refused": 0, "deferred": 0}
    assert len(sink.messages) == 1
    sender, rcpts, _ = sink.messages[0]
    assert sender == "dc@example.test"
    assert rcpts == ["a@example.test", "b@example.test"]
    msg = sink.parsed()
    assert msg["From"] == "dc@example.test"
    assert msg["To"] == "a@example.test, b@example.test"
    assert msg["Subject"] == "orders 2.3.0 deprecated"
    assert msg["Date"] and msg["Message-ID"].endswith("@example.test>")
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content().splitlines() == ["Line 1", "Line 2"]


async def test_without_a_token_the_route_is_401_and_nothing_is_sent(
    sink: SmtpSink,
    make_app: Callable[..., Any],
    asgi_client: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]],
) -> None:
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=BODY)
    assert response.status_code == 401
    assert sink.messages == []


async def test_a_principal_the_pdp_refuses_gets_403_and_nothing_is_sent(
    sink: SmtpSink,
    make_app: Callable[..., Any],
    fake_issuer: FakeIssuer,
    asgi_client: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]],
) -> None:
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer, "someone"))
    assert response.status_code == 403
    assert sink.messages == []


# --- Task 2: bad input, SMTP failures, TLS modes and logging ------------------------------------

Client = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


def _body(**changes: Any) -> dict[str, Any]:
    body = dict(BODY)
    body.update(changes)
    return body


INVALID_BODIES: list[tuple[str, dict[str, Any]]] = [
    ("extra-from", _body(**{"from": "evil@example.test"})),
    ("extra-headers", _body(headers={"Bcc": "evil@example.test"})),
    ("crlf-subject", _body(subject="x\r\nBcc: evil@example.test")),
    ("subject-201", _body(subject="s" * 201)),
    ("empty-subject", _body(subject="")),
    ("recipients-21", _body(to=[f"u{i}@example.test" for i in range(21)])),
    ("recipients-empty", _body(to=[])),
    ("not-an-address", _body(to=["not-an-address"])),
    ("no-dot-domain", _body(to=["a@b"])),
    ("address-321", _body(to=["a" * 64 + "@" + "a." * 127 + "aa"])),
    ("crlf-address", _body(to=["a@example.test\r\nBcc: evil@example.test"])),
    ("nul-in-text", _body(text="hello\x00world")),
    ("empty-text", _body(text="")),
]


@pytest.mark.parametrize("name,payload", INVALID_BODIES, ids=[n for n, _ in INVALID_BODIES])
async def test_invalid_input_is_a_422_validation_error_that_sends_nothing_and_echoes_nothing(
    name: str,
    payload: dict[str, Any],
    sink: SmtpSink,
    make_app: Callable[..., Any],
    fake_issuer: FakeIssuer,
    asgi_client: Client,
) -> None:
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=payload, headers=_auth(fake_issuer))
    assert response.status_code == 422, response.text
    problem = response.json()
    assert problem["type"] == "urn:dc:problem:validation-error"
    assert sink.messages == []
    for needle in ("evil", "Bcc", "hello", "not-an-address", "sssss", "a@b"):
        assert needle not in response.text


async def test_a_subject_with_cr_and_lf_is_422_and_the_sink_stays_empty(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    payload = _body(subject="orders\r\nBcc: evil@example.test")
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=payload, headers=_auth(fake_issuer))
    assert response.status_code == 422
    assert sink.messages == []


async def test_a_body_over_64_kib_is_422_before_it_is_parsed(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    raw = b'{"to":["a@example.test"],"subject":"s","text":"' + b"x" * 65536 + b'"}'
    assert len(raw) > 65536
    headers = {**_auth(fake_issuer), "Content-Type": "application/json"}
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, content=raw, headers=headers)
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dc:problem:validation-error"
    assert sink.messages == []


async def test_a_streamed_body_over_the_cap_is_422_without_a_content_length(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(70):
            yield b"x" * 1024

    headers = {**_auth(fake_issuer), "Content-Type": "application/json"}
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, content=chunks(), headers=headers)
    assert response.status_code == 422
    assert sink.messages == []


async def test_a_non_json_body_is_422_and_is_not_echoed(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, content=b"this is not json", headers=_auth(fake_issuer))
    assert response.status_code == 422
    assert "this is not json" not in response.text


async def test_a_refused_recipient_is_counted_and_the_answer_is_still_200(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    sink.refuse = {"b@example.test"}
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "refused": 1, "deferred": 0}
    assert sink.messages[0][1] == ["a@example.test"]


async def test_a_temporarily_refused_recipient_is_counted_as_deferred(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    sink.defer = {"b@example.test"}
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "refused": 1, "deferred": 1}


async def test_every_recipient_temporarily_refused_is_all_deferred(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    sink.defer = {"a@example.test", "b@example.test"}
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "refused": 2, "deferred": 2}


async def test_every_recipient_refused_is_200_with_zero_accepted(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    sink.refuse = {"a@example.test", "b@example.test"}
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "refused": 2, "deferred": 0}


def _assert_unavailable(response: httpx.Response) -> None:
    assert response.status_code == 503
    problem = response.json()
    assert problem["type"] == "urn:dc:problem:service-unavailable"
    assert problem["detail"] == "Mail delivery is temporarily unavailable."


async def test_a_down_smtp_server_is_503(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    app = make_app(sink)
    sink.stop()  # nothing listens on the port any more: connection refused
    async with asgi_client(app) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    _assert_unavailable(response)


async def test_no_smtp_host_is_503(
    make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    async with asgi_client(make_app(None, smtp_host=None)) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    _assert_unavailable(response)


async def test_an_smtp_error_reply_is_503(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    sink.mail_from_reply = "421 try later"
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    _assert_unavailable(response)
    assert sink.messages == []


class _FakeSmtp:
    """Records the calls of one send; stands in for smtplib.SMTP and SMTP_SSL."""

    calls: list[str]

    def __init__(self, calls: list[str], kind: str, contexts: list[Any] | None = None) -> None:
        self.calls = calls
        self.contexts = contexts if contexts is not None else []
        calls.append(kind)

    def __enter__(self) -> _FakeSmtp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, **kwargs: Any) -> None:
        self.calls.append("starttls")
        self.contexts.append(kwargs.get("context"))

    def close(self) -> None:
        self.calls.append("close")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, message: Any, to_addrs: Any = None) -> dict[str, Any]:
        self.calls.append("send_message")
        return {}


@pytest.fixture
def smtp_contexts() -> list[Any]:
    """Every TLS context the fakes were handed (SMTP_SSL and starttls ``context=``)."""
    return []


@pytest.fixture
def smtp_calls(monkeypatch: pytest.MonkeyPatch, smtp_contexts: list[Any]) -> list[str]:
    calls: list[str] = []

    def _smtp(host: str, port: int, **kw: Any) -> _FakeSmtp:
        return _FakeSmtp(calls, "SMTP", smtp_contexts)

    def _smtp_ssl(host: str, port: int, **kw: Any) -> _FakeSmtp:
        smtp_contexts.append(kw.get("context"))
        return _FakeSmtp(calls, "SMTP_SSL", smtp_contexts)

    monkeypatch.setattr("smtplib.SMTP", _smtp, raising=True)
    monkeypatch.setattr("smtplib.SMTP_SSL", _smtp_ssl, raising=True)
    return calls


async def _send(host: str = "mail.example.test", **settings: Any) -> None:
    from dc_mail.smtp import build_message, send

    cfg = MailSettings(smtp_host=host, smtp_from="dc@example.test", **settings)
    message = build_message(cfg, ["a@example.test"], "s", "t")
    await send(cfg, message, ["a@example.test"])


def _assert_verified(context: Any) -> None:
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_verified_tls_context_checks_the_certificate_the_host_and_the_version() -> None:
    from dc_mail.smtp import verified_tls_context

    _assert_verified(verified_tls_context())


async def test_implicit_tls_uses_smtp_ssl_and_no_starttls(
    smtp_calls: list[str], smtp_contexts: list[Any]
) -> None:
    await _send(smtp_tls="implicit")
    assert smtp_calls == ["SMTP_SSL", "send_message"]
    assert len(smtp_contexts) == 1
    _assert_verified(smtp_contexts[0])


async def test_starttls_runs_before_login_with_the_verified_context(
    smtp_calls: list[str], smtp_contexts: list[Any]
) -> None:
    await _send(smtp_tls="starttls", smtp_username="u", smtp_password="pw")  # noqa: S106
    assert smtp_calls == ["SMTP", "starttls", "login:u:pw", "send_message"]
    assert len(smtp_contexts) == 1
    _assert_verified(smtp_contexts[0])


async def test_a_failing_starttls_closes_the_connection_and_is_a_503_code(
    smtp_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from dc_mail.smtp import SmtpUnavailable

    def _refuse(self: _FakeSmtp, **kwargs: Any) -> None:
        raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")

    monkeypatch.setattr(_FakeSmtp, "starttls", _refuse)
    with pytest.raises(SmtpUnavailable) as excinfo:
        await _send(smtp_tls="starttls")
    assert excinfo.value.code == "smtp-error"
    assert smtp_calls == ["SMTP", "close"]


async def test_tls_none_never_starts_tls(smtp_calls: list[str], smtp_contexts: list[Any]) -> None:
    await _send(host="mailpit", smtp_tls="none")
    assert smtp_calls == ["SMTP", "send_message"]
    assert smtp_contexts == []


@pytest.mark.parametrize(
    "host",
    [
        "mailpit",
        "MAILPIT",
        "localhost",
        "127.0.0.1",
        "127.5.5.5",
        "::1",
        "[::1]",
        "mailpit.contractum.svc",
        "mailpit.contractum.svc.cluster.local",
    ],
)
def test_plaintext_smtp_is_accepted_for_internal_hosts(host: str) -> None:
    assert is_internal_smtp_host(host)
    assert MailSettings(smtp_host=host, smtp_tls="none").smtp_host == host


@pytest.mark.parametrize(
    "host",
    [
        "mail.example.test",
        "smtp.example.com",
        "10.0.0.5",
        "8.8.8.8",
        "2606:4700::1111",
        "fd00::5",
        "[fd00::5]",
        "mailpit.",
        "mailpit.example.com.svc.evil.test",
        "mail pit",
        "mailpit:25",
        "mail/pit",
        "fe80::1%eth0",
        "",
    ],
)
def test_plaintext_smtp_is_refused_for_everything_else(host: str) -> None:
    assert not is_internal_smtp_host(host)
    if host:
        with pytest.raises(ValidationError, match="DC_SMTP_TLS=none is allowed only"):
            MailSettings(smtp_host=host, smtp_tls="none")


@pytest.mark.parametrize("tls", ["starttls", "implicit"])
@pytest.mark.parametrize("host", ["mail.example.test", "smtp.example.com", "10.0.0.5"])
def test_verified_tls_modes_accept_any_host(tls: str, host: str) -> None:
    assert MailSettings(smtp_host=host, smtp_tls=tls).smtp_host == host  # type: ignore[arg-type]


def test_plaintext_smtp_never_carries_credentials() -> None:
    with pytest.raises(ValidationError, match="credentials are never sent without TLS") as info:
        MailSettings(
            smtp_host="mailpit",
            smtp_tls="none",
            smtp_username="u",
            smtp_password="s3cret-pw",  # noqa: S106
        )
    # the message itself is fixed text; pydantic's own repr echoes the raw input, as for any field
    assert all("s3cret-pw" not in error["msg"] for error in info.value.errors())


@pytest.mark.parametrize(
    "creds",
    [{"smtp_username": "u"}, {"smtp_password": "pw"}],
    ids=["user-only", "password-only"],
)
def test_a_half_configured_login_is_refused_at_settings_validation(creds: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="smtp_username and smtp_password must be set together"):
        MailSettings(smtp_host="mail.example.test", **creds)


def test_no_login_and_a_full_login_are_both_valid() -> None:
    assert MailSettings(smtp_host="mail.example.test").smtp_username is None
    cfg = MailSettings(smtp_username="u", smtp_password="pw")  # noqa: S106
    assert cfg.smtp_username == "u"


def test_the_smtp_password_is_read_from_the_secret_file_never_the_environment(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "smtp_password").write_text("from-file", encoding="utf-8")
    monkeypatch.setattr(MailSettings, "secrets_dir", str(tmp_path))
    monkeypatch.setenv("DC_SMTP_PASSWORD", "from-env")
    password = MailSettings(smtp_username="u").smtp_password
    assert password is not None and password.get_secret_value() == "from-file"
    (tmp_path / "smtp_password").unlink()
    assert MailSettings().smtp_password is None


async def test_logs_carry_counts_and_an_outcome_and_never_content_or_secrets(
    sink: SmtpSink,
    make_app: Callable[..., Any],
    fake_issuer: FakeIssuer,
    asgi_client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smtplib.SMTP, "starttls", lambda self, **kw: (220, b"ready"))
    monkeypatch.setattr(smtplib.SMTP, "login", lambda self, user, password: (235, b"ok"))
    app = make_app(
        sink,
        smtp_tls="starttls",
        smtp_username="mailer",
        smtp_password="s3cret-pw",  # noqa: S106
    )
    sink.refuse = {"b@example.test"}
    headers = _auth(fake_issuer)
    async with asgi_client(app) as client:
        with capture_logs() as ok_logs:
            await client.post(SEND, json=BODY, headers=headers)
        sink.mail_from_reply = "421 try later"
        with capture_logs() as failed_logs:
            await client.post(SEND, json=BODY, headers=headers)
    sent = [e for e in ok_logs if e["event"] == "mail_sent"]
    failed = [e for e in failed_logs if e["event"] == "mail_send_failed"]
    assert len(sent) == 1 and len(failed) == 1
    assert sent[0]["recipient_count"] == 2
    assert (sent[0]["accepted"], sent[0]["refused"], sent[0]["outcome"]) == (1, 1, "partial")
    assert sent[0]["principal_id"] == NOTIFIER and sent[0]["request_id"]
    assert failed[0]["outcome"] == "smtp-421" and failed[0]["recipient_count"] == 2
    everything = repr(ok_logs) + repr(failed_logs)
    for secret in ("a@example.test", "b@example.test", "orders 2.3.0", "Line 1", "s3cret-pw"):
        assert secret not in everything


async def test_an_address_of_exactly_320_characters_is_accepted(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    address = "a" * 64 + "@" + ".".join(["b" * 63] * 4)
    assert len(address) == 320
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=_body(to=[address]), headers=_auth(fake_issuer))
    assert response.status_code == 200
    assert sink.messages[0][1] == [address]


@pytest.mark.parametrize(
    "raw",
    [b"\xff\xfe\x00", b'["a@example.test"]', b"null", b"{}"],
    ids=["invalid-utf8", "json-array", "json-null", "empty-object"],
)
async def test_other_malformed_bodies_are_422(
    raw: bytes,
    sink: SmtpSink,
    make_app: Callable[..., Any],
    fake_issuer: FakeIssuer,
    asgi_client: Client,
) -> None:
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, content=raw, headers=_auth(fake_issuer))
    assert response.status_code == 422
    assert sink.messages == []


async def test_non_ascii_text_a_long_line_and_a_dot_line_survive_delivery(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    text = "Grüße, Café\n" + "x" * 5000 + "\n.\nlast"
    payload = _body(subject="Vertrag geändert", text=text)
    async with asgi_client(make_app(sink)) as client:
        response = await client.post(SEND, json=payload, headers=_auth(fake_issuer))
    assert response.status_code == 200
    msg = sink.parsed()
    assert msg["Subject"] == "Vertrag geändert"
    assert msg.get_content().splitlines() == text.splitlines()


async def test_a_rejected_login_is_503_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch,
    sink: SmtpSink,
    make_app: Callable[..., Any],
    fake_issuer: FakeIssuer,
    asgi_client: Client,
) -> None:
    def _refuse(self: smtplib.SMTP, user: str, password: str, **kw: Any) -> None:
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib.SMTP, "starttls", lambda self, **kw: (220, b"ready"))
    monkeypatch.setattr(smtplib.SMTP, "login", _refuse)
    app = make_app(
        sink,
        smtp_tls="starttls",
        smtp_username="mailer",
        smtp_password="s3cret-pw",  # noqa: S106
    )
    async with asgi_client(app) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    _assert_unavailable(response)
    assert "s3cret-pw" not in response.text
    assert sink.messages == []


# --- quick 261120-0yd: the sender comes from the platform settings store (D-26) ------------------


class IdentityStub:
    """Identity's runtime settings route: serves ``sections`` or fails, and records the bearers."""

    def __init__(self, sender: dict[str, Any] | None = None, *, down: bool = False) -> None:
        self.sender = sender
        self.down = down
        self.bearers: list[str] = []

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if self.down:
                raise httpx.ConnectError("identity is unreachable", request=request)
            self.bearers.append(request.headers["authorization"])
            assert request.url.path == "/internal/v1/settings/runtime"
            assert request.url.params["sections"] == "notifications"
            section: dict[str, Any] = {} if self.sender is None else {"sender": self.sender}
            return httpx.Response(200, json={"sections": {"notifications": section}})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_the_stored_sender_name_and_address_are_the_from_header(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    identity = IdentityStub({"name": "Data, Contracts", "address": "no-reply@corp.example.test"})
    app = make_app(sink, http_client=identity.client())
    headers = _auth(fake_issuer)
    async with asgi_client(app) as client:
        response = await client.post(SEND, json=BODY, headers=headers)
    assert response.status_code == 200, response.text
    envelope_sender, _, _ = sink.messages[0]
    assert envelope_sender == "no-reply@corp.example.test"
    msg = sink.parsed()
    assert msg["From"].addresses[0].display_name == "Data, Contracts"
    assert msg["From"].addresses[0].addr_spec == "no-reply@corp.example.test"
    assert msg["Message-ID"].endswith("@corp.example.test>")
    # The request's own bearer is what identity was asked with.
    assert identity.bearers == [headers["Authorization"]]


async def test_a_stored_name_without_an_address_keeps_the_configured_address(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    identity = IdentityStub({"name": "Contractum"})
    async with asgi_client(make_app(sink, http_client=identity.client())) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    assert response.status_code == 200
    msg = sink.parsed()
    assert msg["From"].addresses[0].display_name == "Contractum"
    assert msg["From"].addresses[0].addr_spec == "dc@example.test"


async def test_without_a_readable_store_the_from_is_dc_smtp_from(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    identity = IdentityStub(down=True)
    async with asgi_client(make_app(sink, http_client=identity.client())) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    assert response.status_code == 200
    assert sink.messages[0][0] == "dc@example.test"
    assert sink.parsed()["From"] == "dc@example.test"


async def test_an_unreadable_store_keeps_the_last_good_sender(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    identity = IdentityStub({"name": "Contractum", "address": "no-reply@corp.example.test"})
    app = make_app(sink, http_client=identity.client(), settings_ttl_s=0.01)
    async with asgi_client(app) as client:
        first = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
        await asyncio.sleep(0.05)  # the cache has expired: identity is asked again and is down
        identity.down = True
        second = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    assert first.status_code == second.status_code == 200
    assert [m[0] for m in sink.messages] == ["no-reply@corp.example.test"] * 2


async def test_an_invalid_stored_sender_falls_back_to_dc_smtp_from(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    identity = IdentityStub({"address": "Name <evil@example.test>"})
    async with asgi_client(make_app(sink, http_client=identity.client())) as client:
        response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    assert response.status_code == 200
    assert sink.messages[0][0] == "dc@example.test"


async def test_a_caller_still_cannot_choose_the_sender_even_with_a_stored_one(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    identity = IdentityStub({"address": "no-reply@corp.example.test"})
    async with asgi_client(make_app(sink, http_client=identity.client())) as client:
        response = await client.post(
            SEND, json=_body(**{"from": "evil@example.test"}), headers=_auth(fake_issuer)
        )
    assert response.status_code == 422
    assert sink.messages == []
    assert "evil" not in response.text


async def test_the_logs_carry_no_address_when_a_sender_is_stored(
    sink: SmtpSink, make_app: Callable[..., Any], fake_issuer: FakeIssuer, asgi_client: Client
) -> None:
    identity = IdentityStub({"name": "Secret Name", "address": "no-reply@corp.example.test"})
    with capture_logs() as logs:
        async with asgi_client(make_app(sink, http_client=identity.client())) as client:
            response = await client.post(SEND, json=BODY, headers=_auth(fake_issuer))
    assert response.status_code == 200
    rendered = repr(logs)
    for needle in ("corp.example", "Secret Name", "a@example.test", "b@example.test"):
        assert needle not in rendered
