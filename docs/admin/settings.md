# Runtime settings

A Global admin can change a small set of platform settings while Contractum runs, without a
redeploy. Open **Administration > Settings** in the web app, or use the API under
`/api/v1/identity/settings`.

## How settings work

- **Helm values stay the defaults.** Identity stores only the values an admin saved (the
  *overrides*), one record per section. A field that was never saved uses the owning service's
  Helm or environment value. To change a default, set the environment variable through the
  service's `extraEnv` in your values (for example `identity.extraEnv`) and run `helm upgrade`.
- **Nothing secret is stored.** Secrets stay in Kubernetes Secrets. The settings pages show only
  metadata about them, such as an expiry date.
- **Who may change them.** Only a Global admin may read and change settings. A change needs an
  interactive sign-in: a personal access token or a service-account key cannot change settings.
- **Every change is audited.** Each effective save writes one audit event (`settings.updated`, or
  `settings.imported` for an import). Saving the same values again writes nothing.
- **Concurrent edits are refused.** A section save through the API needs `If-Match` with the
  section's ETag (`"<section>-r<revision>"`), so two admins cannot overwrite each other.
- **Bounds always apply.** Each setting has limits (listed below). A value outside them is refused.
- **Reset to default.** Each field has a reset button that removes the override.

### When a change takes effect

Identity caches its own reads for 5 seconds. The other services read the sections they need from
identity and cache them for 30 seconds. If identity cannot answer, a service keeps its last good
value. It uses its Helm or environment default only when it never read the section. The audit
retention is the exception: audit takes it from the newest settings event in its own log.

| Section | Read by |
|---------|---------|
| Entra | identity |
| Notifications | contracts and mail |
| Governance | contracts and validation |
| Sign-in & security | identity only (never sent to another service) |
| Platform | identity serves the banner; audit reads the retention from the settings events it already stores |

## Entra

| Setting | Default | Notes |
|---------|---------|-------|
| Default tenant | the only configured tenant, if there is exactly one | Must be a configured tenant id (GUID shape). |
| Default owner for registered Entra identities | none | The folder that owns new Entra registrations by default. |

The Entra overview on this tab (tenants, issuers, the Graph client, secret expiry, a connection
test) is view only. Which issuers are trusted stays in Helm (`auth.issuers`). See
[Microsoft Entra ID setup](entra-setup.md).

## Notifications

A change reaches the delivery worker, the sunset reminders and the mail service within about 30
seconds.

| Setting | Default (environment variable) | Bounds |
|---------|-------------------------------|--------|
| Email templates (subject and body per event type) | built-in texts | Subject 1 to 200 characters, body 1 to 8000. Only the listed `{placeholders}`, plain substitution. |
| Sender name and address | mail `DC_SMTP_FROM`, set by the chart from `smtp.from` | Name up to 100 characters. |
| Retry schedule (seconds between attempts) | contracts `DC_NOTIFY_RETRY_SCHEDULE_S` (60, 300, 1800, 7200, 21600, 43200) | 1 to 10 entries, each 10 to 86400. |
| Maximum attempts | contracts `DC_NOTIFY_MAX_ATTEMPTS` (7) | 1 to 20. |
| Sunset reminder lead times (days) | contracts `DC_SUNSET_LEAD_DAYS` (30 and 7) | 1 to 5 entries, each 1 to 365, no duplicates. |
| Allowed webhook hosts | empty | Up to 100 entries: an exact host or `*.example.com`. |

**Allowed webhook hosts.**

- The list is required. An empty list allows **no** webhook host.
- At its first start, contracts fills the list once from the hosts of existing webhook channels.
  A host you remove later is never put back.
- The list only narrows. Webhooks still must use HTTPS and a public address. HTTPS is shown but
  cannot be turned off here.
- `DC_WEBHOOK_ALLOWED_PRIVATE_HOSTS` (chart `notifications.webhookAllowedPrivateHosts`) can only
  be set in Helm. It loosens the rules against private addresses, so the web app can never widen
  it.
- If contracts cannot read the list at all, it fails closed. Saving a card with a webhook URL
  answers 503 `settings-unavailable`, and webhook deliveries wait a minute without using up an
  attempt.

**Test email.** The tab can send a test email through the mail service. It is limited to 5 per 10
minutes per admin, and the audit event records only the recipient's domain.

## Governance

| Setting | Default (environment variable) | Bounds |
|---------|-------------------------------|--------|
| Grace period for new mandatory rule-set versions (days) | validation `DC_MANDATORY_WARN_DAYS` (14) | 0 to 365. |
| Default ODCS version for new contracts | contracts `DC_DEFAULT_ODCS_API_VERSION` (`v3.2.0`) | A version this deployment supports. Otherwise the environment value applies. |
| Require a reason to unpublish or deprecate | contracts `DC_REQUIRE_LIFECYCLE_REASON` (off) | On or off. |

The grace period is read when a rule set becomes mandatory and when a version of a mandatory set
is published. The database migrations that create the built-in rule sets read only the
environment value. The chart's migration Job does not pass `extraEnv`, so on a first install
those built-in sets use the default of 14 days.

## Sign-in & security

| Setting | Default (identity environment variable) | Bounds |
|---------|----------------------------------------|--------|
| Local sign-in | on | A saved setting can turn it off. |
| Microsoft sign-in | on when an Entra issuer is configured | A saved setting can turn it off, never on without an Entra issuer. |
| Access token ceiling (personal access token and key lifetime, days) | `DC_CREDENTIAL_MAX_TTL_DAYS` (365) | 1 to 3650. Existing tokens are unchanged. |
| Active tokens per owner | `DC_MAX_ACTIVE_TOKENS` (10) | 1 to 1000. Counts live personal access tokens per user and keys per service account. |
| Maximum sessions per user | `DC_MAX_SESSIONS_PER_USER` (5) | 1 to 100. |
| Session length (seconds) | `DC_SESSION_MAX_S` (604800, 7 days) | 3600 to 7776000, counted from sign-in. |
| Idle timeout (seconds) | `DC_REFRESH_TOKEN_TTL_S` (43200, 12 hours) | 900 to 604800, and at most the session length. |
| Lockout: failed sign-ins per account | `DC_THROTTLE_ACCOUNT_THRESHOLD` (5) | 3 to 20. |
| Lockout: failed sign-ins per IP address | `DC_THROTTLE_IP_THRESHOLD` (20) | 10 to 500. |
| Lockout: longest lock (seconds) | `DC_THROTTLE_MAX_LOCK_S` (900) | 60 to 86400. |
| Password minimum length | 12 | 12 to 128. |
| Password needs an uppercase letter, a lowercase letter, a digit, a symbol | off | Four separate switches. |

- **Sessions.** A password sign-in beyond the session limit ends that user's oldest session (one
  `user.sessions_evicted` audit event). The sign-in itself is never refused. A lowered limit
  changes nothing until the user's next sign-in.
- **Passwords.** The rules apply to new passwords only (a password change or a new user), never
  at sign-in.

### Sign-in lockout safety

Saving or importing this section is refused (422 or 409 `signin-lockout-risk`, no audit event)
when it would:

- turn off both sign-in methods;
- turn off the method the acting admin is signed in with;
- turn off local sign-in when there is no configured Entra issuer and no active Entra Global admin;
- turn off Microsoft sign-in when there is no active local Global admin.

When a method is off:

- local sign-in and session refresh answer 403 `sign-in-method-disabled`, before the password is
  checked and without counting a failure;
- Entra user tokens are refused with the same problem.

Personal access tokens, service-account keys and Entra service principals are not sign-ins and
keep working. Access tokens that were already issued expire within their normal lifetime
(15 minutes).

### Locked out of sign-in

If a mistaken change leaves nobody able to sign in:

1. Force local sign-in on and restart identity:

   ```bash
   helm upgrade dc deploy/helm/contractum -n contractum --reuse-values \
     --set identity.extraEnv.DC_SIGNIN_LOCAL_FORCE_ENABLED=true
   ```

2. Sign in with a local Global admin (the bootstrap admin, or any other local Global admin).
3. Open **Administration > Settings > Sign-in & security**, turn local sign-in back on (or fix
   whatever else is wrong) and save. While the force switch is on, a method that is already off
   may stay off in a save.
4. Remove the variable again: run `helm upgrade` with your values file and without
   `DC_SIGNIN_LOCAL_FORCE_ENABLED`.

## Platform

| Setting | Default (environment variable) | Bounds |
|---------|-------------------------------|--------|
| Announcement banner | none | Text 1 to 500 characters, severity `info` or `warning`, optional start and end time. |
| Audit retention (days) | audit `DC_RETENTION_DAYS` (unset: keep every event) | `0` keeps every event, otherwise 90 to 36500. |

The **About** tab is view only. It shows each service's version, commit, build time, database
state and migration head. Identity reads it from the other services, which the default
NetworkPolicies already allow.

### Audit retention

- The audit service deletes events older than the effective retention. It runs every
  `DC_RETENTION_INTERVAL_S` (3600) seconds, in batches of `DC_RETENTION_BATCH` (5000), at most
  `DC_RETENTION_MAX_BATCHES` (100) batches per run. A database lock makes sure only one replica
  works at a time.
- The effective value is the newest saved setting, otherwise `DC_RETENTION_DAYS`. It is never
  below `DC_RETENTION_MIN_DAYS` (90). The service refuses to start with a floor below 30.
- Settings events are **never** deleted, so the job cannot erase its own configuration.
- Each run that deleted events writes one `audit.retention_applied` event (retention days,
  cutoff, count).
- Set these variables on the audit service with `audit.extraEnv`.

## Export and import

- **Export** downloads a versioned `contractum-settings` JSON file of the saved overrides. It holds
  no secrets.
- **Import** shows the differences first (a dry run), then applies them as one audited event. The
  same checks as a normal save apply, including the sign-in lockout safety.
