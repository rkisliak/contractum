"""SMTP delivery: one plain-text message, stdlib ``smtplib`` in a worker thread, no retries.

Nothing here logs or raises with the password, the addresses, the subject or the body: a failure
is reported as a short ``code`` on :class:`SmtpUnavailable` and nothing else (D-105).
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from dc_core.mail import MailSendOut

from dc_mail.settings import MailSettings


class SmtpUnavailable(Exception):  # noqa: N818 - name fixed by the plan interface
    """Mail cannot be delivered right now. ``code`` is ``smtp-not-configured``, ``smtp-error``
    or ``smtp-<status>`` (the SMTP reply code), never anything the caller sent."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_message(
    settings: MailSettings,
    to: Sequence[str],
    subject: str,
    text: str,
    *,
    from_header: str | None = None,
    from_address: str | None = None,
) -> EmailMessage:
    """The message: From is fixed by the service, never by the caller.

    ``from_header`` is the sender the service itself resolved from the platform settings store
    (``name <address>``, or just the address); without it ``DC_SMTP_FROM`` is used.
    """
    message = EmailMessage()
    address = from_address or settings.smtp_from
    message["From"] = from_header or settings.smtp_from
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=address.rpartition("@")[2] or None)
    message.set_content(text)
    return message


def verified_tls_context() -> ssl.SSLContext:
    """The ONE TLS context of the mail service, for implicit TLS and STARTTLS alike.

    The server certificate is always verified against the system trust store and must match the
    host name, and nothing older than TLS 1.2 is spoken. Both properties are set explicitly so a
    change of the library defaults can never loosen them.
    """
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _connect(settings: MailSettings, context: ssl.SSLContext) -> smtplib.SMTP:
    """Open the connection for the configured TLS mode; every TLS mode uses ``context``."""
    assert settings.smtp_host is not None  # noqa: S101 - checked by send()
    host, port, timeout = settings.smtp_host, settings.smtp_port, settings.smtp_timeout_s
    if settings.smtp_tls == "implicit":
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    client = smtplib.SMTP(host, port, timeout=timeout)
    if settings.smtp_tls == "starttls":
        try:
            client.starttls(context=context)
        except BaseException:
            client.close()
            raise
    # smtp_tls == "none" (the plaintext dev mode): reachable only for a host the MailSettings
    # validator accepted (loopback, single-label or cluster .svc name) and never with credentials.
    return client


def _send_sync(
    settings: MailSettings, message: EmailMessage, recipients: Sequence[str]
) -> dict[str, tuple[int, bytes]]:
    with _connect(settings, verified_tls_context()) as smtp:
        if settings.smtp_username and settings.smtp_password:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        return smtp.send_message(message, to_addrs=list(recipients))


def _deferred(refusals: Mapping[str, tuple[int, bytes]]) -> int:
    """How many refusals are temporary (SMTP 4xx), so a later attempt could still succeed."""
    return sum(1 for code, _reply in refusals.values() if 400 <= code < 500)


async def send(
    settings: MailSettings, message: EmailMessage, recipients: Sequence[str]
) -> MailSendOut:
    """Send ``message`` to ``recipients``; a refused recipient is a count, not an error."""
    if not settings.smtp_host:
        raise SmtpUnavailable("smtp-not-configured")
    try:
        refused = await asyncio.to_thread(_send_sync, settings, message, recipients)
    except smtplib.SMTPRecipientsRefused as exc:
        return MailSendOut(accepted=0, refused=len(recipients), deferred=_deferred(exc.recipients))
    except smtplib.SMTPResponseException as exc:
        raise SmtpUnavailable(f"smtp-{exc.smtp_code}") from None
    except (smtplib.SMTPException, OSError):
        raise SmtpUnavailable("smtp-error") from None
    return MailSendOut(
        accepted=len(recipients) - len(refused),
        refused=len(refused),
        deferred=_deferred(refused),
    )
