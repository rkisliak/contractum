---
name: subscriber-cards
description: Change subscriber cards, members, version pins, the owner view, lifecycle guards, notices, webhook and email delivery or the sunset scheduler without leaking card existence, secrets or access.
---

# subscriber-cards

Use when touching `contracts/src/dc_contracts/{cards,subscriptions,pins,notices,notice_consumer,webhooks,delivery,mailer,scheduler}.py`,
`api/cards.py`, the owner-view routes in `api/versions.py`, or migrations `0009` to `0013` and `0016`.
The version lifecycle itself lives in `contract-lifecycle` (`../contract-lifecycle/SKILL.md`);
this skill covers what cards add on top. The mail service has its own skill
(`mail/.claude/skills/mail-service/SKILL.md`).

1. **Capability resolution** (`cards.card_capability`). One `folder:read` decision gives `read`
   (folder readers) and `manage` (the card's creator or a `card:manage` holder). Membership adds
   `read` and `subscribe`, never `manage`. The route gates are principal-level (`card:read`,
   `card:write`, `card:manage-secret`), so a full identity outage fails `require()` and is **503
   for every card id**, known or not. After that gate, a lookup that fails before the caller has
   been shown the card (the folder decision, or the membership lookup for a non-reader) fails
   closed to "no capability", which is the **uniform 404**: a 503 for a known id beside a 404 for
   an unknown one would reveal which ids exist. Only a folder reader, who already sees the card,
   gets a 503 from the membership lookup. Never add a code path that tells an unreadable card from
   a missing one. Tests: `uv run pytest -m integration contracts/tests/integration/test_cards.py`.
   Denials (WR-02): every non-ALLOW `folder:read` check is a recorded denial, so for `need`
   `read` and `subscribe` membership is asked first, and a linked member whom
   `accessible_folders(folder:read)` excludes is answered without the folder check (no false
   `not_found` denial for a successful member read). A member write refused with 403 makes the
   check in `_managed_card`, so the refusal is recorded. Never put the folder check back in
   front of membership.
2. **Members versus managers** (D-85, `api/cards.py`, table `card_member`). A manager links local
   groups, directory groups and service accounts only after identity's `members/check` agrees
   (422 `invalid-member`; a partial or missing answer is `AuthorizerUnavailable`, never read as a
   boolean). Write routes resolve the capability with `need="read"`, so a linked member who is not
   a manager gets 403, not 404. Add and remove lock the card `FOR UPDATE` first. Test:
   `contracts/tests/integration/test_card_members.py`.
3. **Pins** (`subscriptions.py`, `pins.py`, table `card_subscription`). Subscribe, move and
   unsubscribe run **under `versions.lock_contract`**, the same lock the lifecycle writes take, so
   a concurrent pin and unpublish cannot both succeed. The D-86 read check (the acting principal's
   own `contract:read-published`) runs before the lock so no row lock is held across a call to
   identity. Rows are history: never deleted, ended once by a trigger (`ended_at`, reason), which
   the database enforces. Pins record dependency, never access (D-88): there is no card-scoped
   fetch. `pins.py` is a leaf (models and lifecycle only); `versions` imports it, never the
   reverse. Tests: `test_cards.py`, `test_pin_guards.py`, and `test_pin_concurrency.py` three
   consecutive runs.
4. **Owner view** (`GET /contracts/{contract_id}/subscribers`, D-102). A `contract:read` holder
   sees, per active pin, only the card's name, description, contacts, pin time, pinner and the pin's subscription id; never
   members, channels, webhook URL or the card's pins on other contracts, whatever its
   organization. The page follows subscription id (uuidv7) order with a keyset cursor, `limit + 1`
   rows decide `next_cursor`, and the grouping by version happens **per page**, so a version can
   continue on the next page and clients merge by `version_id`. Removing a pin
   (`POST .../subscriptions/{subscription_id}/remove`) needs `contract:publish` and a stripped
   1..500 character reason; removing an already ended pin is a 200 no-op that keeps the earlier
   end reason. Tests: `test_pin_guards.py`, the owner-view cases in `test_card_notices.py`.
5. **Lifecycle guards** (`versions.unpublish_version`, `versions.delete_contract`; see
   `contract-lifecycle` item 7). Both collect every blocker, then raise one `LifecycleBlocked`
   (a `ProblemError`, 409, slug `reasons[0].code`). Order: unpublish lists `latest-version-protected`
   then `version-pinned`; delete lists `contract-referenced` then `contract-pinned` (D-106). Pinned
   cards are named across organizations (D-95). A refused unpublish **rolls the unit back first**,
   then writes `contract.version_unpublish_blocked` on the same session (never inside the locked
   unit), and only when pinned cards exist. A blocked delete records nothing. Test: `test_pin_guards.py`.
6. **Where notices come from** (D-99 as amended). `publish`, `promote` and `deprecate` read
   `notify_card_ids` after taking their locks (`notices.audience_for_*`, one indexed read) and put
   it in their own audit event; the blocked-unpublish event carries `notify_card_ids` too, and the
   `card.subscription_removed` event's aggregate is the card itself. Nothing is written to a notice table inside a lifecycle unit and nothing calls a channel. The
   durable JetStream consumer `contracts-notices` (`notice_consumer.py`, stream AUDIT, filter
   `audit.contracts.>`, permissions pinned in `infra/nats/nats.conf.tmpl`) feeds
   `notices.handle_payload`, which materialises one `card_notice` per card and event with
   per-channel `card_notice_delivery` rows, exactly once (dedupe key unique). A lifecycle event
   without `notify_card_ids` (written before this phase) is ignored; a malformed event
   (`InvalidNoticeEvent`) is logged and acked; any other error naks with
   `min(2**deliveries, DC_NOTICE_CONSUMER_MAX_BACKOFF_S)` and is dropped, logged, after
   `DC_NOTICE_CONSUMER_MAX_DELIVER`. The blocked-unpublish notice is throttled per card and
   version by `DC_BLOCKED_NOTICE_COOLDOWN_S`. Tests: `test_card_notices.py`,
   `test_notice_consumer_nats.py` (add a refusal case there first when the consumer's filter or
   permissions change).
7. **Delivery worker** (`delivery.py`, `CardNotifier`, started by `notifier_hook` only when
   `DC_NOTIFY_WORKER_ENABLED`). One replica delivers (session advisory lock `dc-card-notifier`).
   Due rows are claimed with `FOR UPDATE SKIP LOCKED` and leased (`DC_NOTIFY_LEASE_S`, 120 by
   default), sent **outside any transaction**, and the result is recorded in a second short
   transaction. Failures retry on `DC_NOTIFY_RETRY_SCHEDULE_S` with jitter up to
   `DC_NOTIFY_MAX_ATTEMPTS`, then `failed`. The worker never writes a card's channel flag, and
   `last_error` is a code from a fixed list, never a response body. Test: `test_delivery_worker.py`.
8. **Webhooks** (`webhooks.py`, SUB-07). Payloads follow Standard Webhooks: `webhook-id`
   `msg_<delivery id>`, `webhook-timestamp`, one `v1` signature per valid secret (current plus the
   previous one inside `DC_WEBHOOK_SECRET_OVERLAP_S`). The fixed envelope fields are merged after
   the notice payload so a payload key cannot override them. **SSRF rules:** the URL is https, has
   no userinfo, and resolves only to globally routable addresses (multicast refused explicitly,
   `is_forbidden_address`) unless the host is in `DC_WEBHOOK_ALLOWED_PRIVATE_HOSTS`. Every attempt
   resolves the host again (`check_resolved_host`; the single DNS seam is `webhooks._resolve`,
   tests patch that attribute), connects to the validated address with `Host` and TLS SNI kept on
   the hostname (`pinned_request_parts`), refuses redirects, and records `ssrf-blocked` or
   `dns-error` without opening a connection. **The allowed-hosts list is mandatory** (quick
   261120-s0p): an empty list, and a worker without the notifier key, allow no host (card PATCH 422
   `webhook-host-not-allowed`, delivery terminal `failed` with the same code, no request). At start-up
   `webhook_host_seed.py` (`DC_WEBHOOK_HOST_SEED_ENABLED`) posts the hosts of the existing webhook
   channels to identity once; until `app.state.webhook_host_seed.done` the worker defers webhook
   deliveries (`settings-unavailable`, no attempt used) and a URL edit is 503. Never put an unlisted
   host back on the allow path, and never read the list with the card owner's data. Tests:
   `contracts/tests/unit/test_webhook_signing.py`, `test_webhook_pinning.py`,
   `contracts/tests/integration/test_card_webhook.py`, `test_delivery_worker.py`,
   `test_webhook_host_seed.py`, `test_settings_runtime.py`.
9. **Secrets** (D-107, D-79). A webhook signing secret (`whsec_...`) is Fernet-encrypted in
   `card_webhook_secret` with the key from the `webhook_secret_key` Docker secret (a malformed key
   fails settings at startup without echoing it; no key means a webhook cannot be enabled, 503).
   It is returned **once**, on the enabling `PATCH` or on `POST /cards/{card_id}/webhook-secret`
   (rotate, with an overlap window). Issuing and rotating need an **interactive sign-in** through
   `card:manage-secret`, an `interactive_only` permission that PATs and service-account keys are
   refused (`dc_auth.roles`). `webhook_url`
   is shown in full only to managers; everyone else sees scheme and host (`_public_webhook_url`).
   Audit events and logs carry `webhook_host` and `webhook_secret_generation`, never the URL or a
   secret. Never add a secret, URL or contact value to `__audit_fields__`, a log line or an error
   body. `test_webhook_secret_scan.py` searches events, logs and responses for planted values.
10. **Email** (`mailer.py`, D-105). The worker's `email` branch renders the subject and plain text
    (`render_email`; control whitespace becomes one space, other controls are removed) and POSTs
    one `dc_core.mail.MailSendIn` to `DC_MAIL_URL` + `/internal/v1/mail/send`, authenticating with
    the `mail_api_key` Docker secret, which is the `contracts-notifier` service account's key.
    The mail service authorizes it through the identity PDP (`mail:send`, ROOT, held by the
    `notifier` role). Contracts never speaks SMTP and the mail service never retries: this worker
    owns the lease, the schedule and the terminal state. A partial refusal is `delivered`;
    codes are listed in `contracts/CLAUDE.md`. Tests: `test_email_render.py`,
    `test_email_delivery.py`.
11. **Sunset scheduler** (`scheduler.py`, D-96, started by `scheduler_hook` only when
    `DC_SUNSET_SCHEDULER_ENABLED`). Every `DC_SUNSET_SCAN_INTERVAL_S`, under the session advisory
    lock `dc-card-sunset`, it records one `contract.version.sunset_approaching` notice per active
    pin per lead in `DC_SUNSET_LEAD_DAYS` through `notices.notify_card`, dedupe key
    `sunset:<subscription_id>:<lead>:<sunset_date>`. Dates are UTC. `pick_lead` returns the
    smallest lead with `days_left <= lead`, and `None` once the sunset has passed, so lead order
    never matters and a missed scan sends the closest lead, not every earlier one. It writes no
    outbox event. Tests: `test_sunset_leads.py`, `test_sunset_notices.py`.
12. **Breaking-change notice** (`notices._breaking_published`, SUB-08, D-162 to D-164, migration
    `0016`). `contract.version.breaking_published` is sent **in addition to**
    `contract.version.published`, never instead. `publish_draft` writes `notify_pins`
    (`[{card_id, version_id}]`, identifiers only) next to `notify_card_ids`, read under the publish
    locks; the consumer diffs each distinct pinned version against the new document with
    `dc_odcs.diff.compare` in `asyncio.to_thread`, never under the publish lock, and looks at no more
    than `MAX_BREAKING_PIN_VERSIONS` (newest first, `truncated_pins` records the rest). The handler
    contract is a **list of `NoticePlan`**, each with its own `dedupe_key`: every existing handler
    returns one plan keyed `event:<event_id>`, the breaking one `event:<event_id>:breaking`, and
    `EVENT_HANDLERS` maps an action to a tuple of handlers (`contract.version_published` has two).
    `plan_breaking_notices` (pure) groups cards by their set of pinned versions and builds the payload
    from an allow-list: new version id and semver, `classifier_version`, `truncated_pins`, and per
    affected pinned version `semver`, `breaking_count`, `changes_total`, at most
    `MAX_BREAKING_CHANGES` (50) changes (`code`, name-keyed `path`, `attribute`, `before`/`after` only
    for `dc_odcs.diff.model.value_allowed`) and `diff_url`; sections are in SemVer precedence order.
    `_FORBIDDEN_PAYLOAD_KEYS` also rejects `description`, `examples`, `quality`, `query` and
    `customProperties`. `mailer._breaking_block` renders at most 50 change lines in all, each through
    `sanitize_header_text` and capped at 300 characters, inside a 10000-character block budget, then
    `and N more, see <diff URL>`; webhooks deliver the payload as it is. Never add document content
    to this payload. Tests: `test_breaking_notice.py` (unit), `test_breaking_notice_flow.py`,
    `test_email_render.py`.
13. **Prove changes.** `uv run pytest -m unit contracts`, then the integration files named above
    (`uv run pytest -m integration contracts/tests/integration/<file>`), `test_pin_concurrency.py`
    three consecutive runs, and the walker `contracts/tests/unit/test_route_policies.py`. End to
    end through the gateway, with Mailpit and the webhook sink:
    `bash scripts/e2e.sh tests/e2e/test_phase41_subscriptions_journey.py`.
