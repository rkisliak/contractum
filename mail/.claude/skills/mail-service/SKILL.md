---
name: mail-service
description: Change the mail service (request contract, refused-recipient and 503 rules, SMTP TLS modes and the smtp_password secret, the counts-only logging rule) without letting a caller choose the sender or leak content.
---

# mail-service

Use when changing `mail/src/dc_mail/` or `dc_core.mail`, or when wiring a new caller of
`POST /internal/v1/mail/send`.

1. **Request contract.** `dc_core.mail.MailSendIn` is `to` (1..20 addresses), `subject` and plain
   `text`, with `extra="forbid"`. There is no sender, Bcc or header field and there must not be
   one: From comes from `MailSettings.smtp_from`. Address, subject and text validators raise fixed
   messages so a 422 never echoes what was submitted. The response is `MailSendOut(accepted,
   refused, deferred)`. A change to either model is a change for every caller; update both sides together.
2. **Refusals are counts, not errors.** A partially or fully refused recipient list
   (`SMTPRecipientsRefused` or a non-empty refused map) is still `200` with the counts. `deferred` is the
   temporary (SMTP 4xx) part of `refused`. The caller decides what a refusal means; the contracts
   worker retries when anything is deferred.
3. **503 mapping.** Any connection, TLS, authentication or SMTP error reply, and a missing
   `DC_SMTP_HOST`, raises `SmtpUnavailable(code)` and becomes `503 service-unavailable` with a fixed
   detail. The service never retries; the contracts delivery worker retries.
4. **TLS and credentials.** `DC_SMTP_TLS` is `none`, `starttls` (default, runs before login) or
   `implicit`. Both TLS modes use the one `verified_tls_context()` in `smtp.py` (certificate required,
   host name checked, TLS 1.2 minimum, passed explicitly to `SMTP_SSL` and `starttls`); never build
   another context. `none` is a dev mode (Mailpit): `MailSettings` refuses it at startup unless
   `is_internal_smtp_host(host)` (loopback, a single-label name such as `mailpit`, `.svc` or
   `.svc.cluster.local`) and refuses it together with a username (no credentials without TLS).
   The check is on the name syntax, not on what the name resolves to: a single-label name relies on
   the resolver search list, so a host with a corporate search domain could resolve it externally;
   an explicit allow-list (for example `DC_SMTP_PLAINTEXT_HOSTS`) is a possible follow-up.
   `tests/repo/test_mail_plaintext_hosts.py` proves compose.yaml and the Helm mailpit branch pass
   the validator. `DC_SMTP_USERNAME` plus the password log in only when both are set; one without the other fails settings
   validation at startup. The password is
   read only from `/run/secrets/smtp_password` (Docker secret); the environment source drops it, and
   it must never be added to compose `environment:`.
5. **Logging rule.** Log request id, principal id, recipient count, accepted, refused and an outcome
   code (`sent`, `partial`, `refused`, `smtp-<status>`, `smtp-error`, `smtp-not-configured`). Never
   addresses, subject, body, SMTP reply text or the password.
6. **Authorization.** The route is `require("mail:send")` on the internal `PolicyRouter`; the body
   is read only after the policy dependency ran. Any new route gets a policy and a walker entry in
   `mail/tests/unit/test_route_policies.py`, and any new problem slug a row in
   `docs/user/api-guide.md` ("Errors: RFC 9457 problem details").
7. **Imports.** Only `dc_core` and `dc_auth`. No database, outbox or audit events here.
8. **Verify.** `uv run pytest -m unit mail shared/dc-core tests/repo`, `uv run mypy mail/src`,
   `uv run lint-imports`. Tests use the in-process SMTP sink in `test_mail_service.py`,
   `structlog.testing.capture_logs` for the logging rule, and the walker for route policies.
