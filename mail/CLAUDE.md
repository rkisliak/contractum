# mail (package `dc_mail`, workspace member `dc-mail`)

Sends one plain-text notification email over SMTP on behalf of other services.

## What this module owns

- `POST /internal/v1/mail/send`: internal only, not in OpenAPI, never routed by the gateway, reachable
  on the backend network only. Wire models `MailSendIn` / `MailSendOut` live in `dc_core.mail`,
  shared with the caller (the contracts delivery worker).
- SMTP delivery through stdlib `smtplib` in a worker thread (`asyncio.to_thread`), TLS modes
  `none`, `starttls` and `implicit`, and `MailSettings` (`DC_SMTP_*`, the password only from the
  Docker secret `/run/secrets/smtp_password`). STARTTLS and implicit TLS share one verified context
  (`verified_tls_context`: certificate and host name checked, TLS 1.2 minimum). `none` is accepted
  only for a loopback, single-label (service name such as `mailpit`) or cluster `.svc` host and
  never with credentials; any other host fails settings validation at startup.
- No database, no outbox, no audit events and no retries. The contracts delivery worker owns
  retries and the record of what was sent; this service answers once per call.

## What it must never import

Services import only the `dc_*` libraries. This module imports `dc_core` and `dc_auth` and nothing
else: never `dc_identity`, `dc_contracts`, `dc_validation` or `dc_audit`. Authorization reaches
identity over HTTP through `dc_auth.remote`. The `dc_*` libraries never import services.
`uv run lint-imports` enforces it.

## Key files and entry points

- `src/dc_mail/main.py`: `create_mail_app` (`uvicorn --factory dc_mail.main:create_mail_app`) and
  the one route `send_mail`.
- `src/dc_mail/smtp.py`: `build_message` (takes the resolved `from_header`), `send`, `SmtpUnavailable` (carries a short `code` only).
- `src/dc_mail/main.py` also holds `resolve_sender` and `settings_client_hook`; mail's about reports `setting_defaults` `{notifications: {sender: {address: DC_SMTP_FROM}}}`.
- `src/dc_mail/settings.py`: `MailSettings`.
- `shared/dc-core/src/dc_core/mail.py`: `MailSendIn`, `MailSendOut`, `MAIL_SEND_PATH`, the caps.

## Tests

- Unit (no Docker; an in-process SMTP sink): `uv run pytest -m unit mail`
- Route policy walker: `mail/tests/unit/test_route_policies.py`.
- Service behaviour: `mail/tests/unit/test_mail_service.py`.
- Types: `uv run mypy mail/src`. Whole repo gate: `bash scripts/ci.sh`.

## Platform rules that apply here

- The route is declared with `require("mail:send")` on a `PolicyRouter` and listed in the walker
  test. Only the notifier service account and Global admins hold `mail:send`.
- From comes from the platform settings store, never from the caller (quick 261120-0yd): the service
  reads `notifications.sender` (`name`, `address`) from identity with the request's own bearer
  (`RuntimeSettingsClient`, 30 s cache `DC_SETTINGS_TTL_S`, last good value kept) and uses
  `DC_SMTP_FROM` for an address that is not stored or cannot be read. A caller cannot choose the
  sender, Bcc or headers: the request model forbids extra fields and control characters in
  addresses and subject.
- Logs carry the request id, principal id and counts plus an outcome code, never addresses, the
  subject, the body or the SMTP password.
- Errors are RFC 9457 problems with `urn:dc:problem:<slug>` types listed in
  `docs/user/api-guide.md` ("Errors: RFC 9457 problem details");
  every slug raised here must be documented there.

## Skills

- `.claude/skills/mail-service/SKILL.md`: request contract, refusal and 503 rules, TLS and secret,
  logging rule, how to test.
- Repo-level skills (run-stack, add-route, add-migration) live in the root `.claude/skills/`.
