# Local stack (Docker Compose)

Docker Compose runs the whole platform on one machine. Use it to try Contractum locally and as the
development and test stack. Production runs on Kubernetes with the Helm chart: see
[Deploy on Kubernetes](../admin/deploy-kubernetes.md).

Run every command on this page from the repository root.

## What you need

| Requirement | Notes |
|---|---|
| Docker Desktop (or Docker Engine) | With the Compose v2 plugin, version 2.21 or newer (`docker compose version`) |
| 6 GiB or more of memory for Docker | Docker Desktop: Settings, Resources. The scripts only warn below 6 GiB |
| Port **8443** free | The gateway is the only published port |
| *(optional)* `curl` and `python3` | Only for the smoke test (`run.sh smoke`). On Windows the smoke test also needs Git Bash or WSL |

You do not need to set any environment variables. On first start, the `dev-init` container
generates every secret, key and the dev TLS certificate. They are stored in a Docker volume, never
in the repository. Entra sign-in works out of the box against a built-in mock tenant (see
[Mock identity provider and fake Graph](#mock-identity-provider-and-fake-graph)).

## Try it locally

`run.sh` and `run.ps1` are the quickstart. They check the prerequisites, then build, start and wait
until every container is healthy.

macOS, Linux, WSL or Git Bash:

```bash
./run.sh
```

Windows PowerShell:

```powershell
.\run.ps1
```

If script execution is blocked, run `powershell -ExecutionPolicy Bypass -File .\run.ps1`.

If something is missing (Docker not running, Compose too old, port 8443 busy), the script stops and
says what to fix. The first run builds the images and takes several minutes.

Both scripts take the same commands:

| Command | What it does |
|---|---|
| *(none)*, `up` or `start` | Checks the prerequisites, then builds, starts and waits until healthy |
| `check` | Checks the prerequisites only |
| `smoke` | Signs in as `admin` through the gateway. On the first run it also rotates the bootstrap password |
| `e2e [args]` or `test [args]` | Runs the end-to-end tests in a container (default `tests/e2e`, about 10 minutes) |
| `status` or `ps` | Shows the containers |
| `logs [service]` | Follows the logs of all services or one |
| `down` or `stop` | Stops and removes the containers. The data is kept |
| `reset` | Deletes all data, the generated secrets and `.dev/` (asks first) |
| `help` | Prints the command list |

The scripts wait up to `WAIT_TIMEOUT` seconds (default `300`) for the stack to become healthy.

## After it starts

- **Web UI:** `https://localhost:8443/`
- **API docs (Swagger UI):** `https://localhost:8443/api/v1/docs/`
- **Merged OpenAPI document:** `https://localhost:8443/api/v1/openapi.json`
- **TLS:** the certificate is signed by a local development CA, `.dev/ca.crt`, that `dev-init`
  creates for this Compose stack only. Trust it with `curl --cacert .dev/ca.crt https://localhost:8443/...`,
  import it into the macOS keychain or the Windows certificate store, or accept the browser warning.
  A Kubernetes deployment does not use this CA: it has its own certificate, see
  [Deploy on Kubernetes](../admin/deploy-kubernetes.md).

### First sign-in

1. The first user is `admin`. Its one-time secret is in `.dev/bootstrap_admin_secret`.
2. You must change the password on first sign-in. `./run.sh smoke` does that for you, saves the new
   password to `.dev/admin_password` (mode 600) and checks that the rotation reached the audit
   trail.
3. Sign in to the web UI as `admin` with the password from `.dev/admin_password`, or choose
   **Sign in with Microsoft** to sign in as the mock tenant's persona Alice.

The files in `.dev/` are git-ignored. Never commit them, and never paste their content.

### Demo users

The UI end-to-end suite (`bash scripts/e2e-ui.sh`) creates non-admin users in the Team folder
`acme/payments/ledger`: an editor, a reader and a subscriber. Their user names and passwords are in
`ui/e2e/personas.ts`. The demo data the journeys read is created by `ui/e2e/demo-seed.ts`.

## Tests

| Command | What it runs |
|---|---|
| `uv run pytest -m unit` | Unit tests. They need nothing else |
| `uv run pytest -m integration` | Integration tests against real Postgres and NATS containers (testcontainers). They need Docker |
| `bash scripts/e2e.sh [pytest args]` | End-to-end tests on a warm stack. It brings the stack up incrementally and never removes containers or volumes |
| `./run.sh e2e [pytest args]` | The same, through the quickstart script |
| `bash scripts/e2e-ui.sh [spec] [playwright args]` | UI end-to-end tests (Playwright) on a warm stack, for example `bash scripts/e2e-ui.sh e2e/smoke.spec.ts -g axe` |
| `bash scripts/skeleton-smoke.sh` | The smoke test that `./run.sh smoke` runs |

### The CI script

`bash scripts/ci.sh` is the one CI entry point. Any CI system runs exactly this script.

| Command | What it runs | When |
|---|---|---|
| `bash scripts/ci.sh` | Lock check, lint, format, types, import rules, unit and integration tests, the UI static checks | Every change. This is the default and it is fast |
| `CI_E2E=1 bash scripts/ci.sh` | The above, then a cold stack (volumes removed) with the UI and API end-to-end tests | Before a larger merge |
| `CI_FULL=1 bash scripts/ci.sh` or `bash scripts/ci.sh --full` | The full gate (see below) | Before a release, not per edit |

Other switches: `CI_INTEGRATION=0` skips the integration tests, and `CI_UI=0` skips the UI step and
the UI end-to-end tests.

The full gate runs everything above plus line and branch coverage with a floor per package, the
timing-sensitive tests on their own, Vitest coverage with thresholds, every Playwright spec and
project, Schemathesis, and the operation coverage gate
(`scripts/operation_coverage.py --gate`). The operation coverage gate needs every API operation to
have a backend test, an authorization refusal test, a row in the generated role and operation
matrix, and a UI screen that calls it, unless it is exempted with a reason in
`tests/repo/operation_coverage_exemptions.json`. The full gate needs Docker and port 8443, and takes
about 2.5 hours on a cold stack. It refuses `CI_E2E=0`, `CI_INTEGRATION=0` and `CI_UI=0`.

It writes its reports to `.coverage-data/` (Python coverage data, `coverage.json`,
`operation-coverage.json`) and `ui/coverage/`. Both are git-ignored.

Tests marked `performance` (throughput and scale budgets) are skipped by every step of
`scripts/ci.sh` and by `scripts/e2e.sh` unless you set `DC_PERFORMANCE=1`. `./run.sh e2e` runs them.
Docker on development hardware usually cannot hold their budgets.

The same steps run on GitHub as workflows: see [CI and GitHub](ci-and-github.md).

## Developing the UI

The UI needs the Node version in `ui/.nvmrc` and pnpm as pinned by `packageManager` in
`ui/package.json`.

- `pnpm --dir ui dev` starts the Vite dev server on `http://127.0.0.1:5173`. It forwards `/api` to
  the running stack on port 8443. Open it as `127.0.0.1`, never as `localhost`: once the browser
  has seen HSTS from `https://localhost:8443`, it upgrades `http://localhost:5173` to HTTPS, which
  the dev server does not speak. Safari refuses the session cookie on the plain-HTTP origin, so use
  Chrome or Firefox, or the gateway URL.
- After a change under `ui/`, rebuild only the UI container: `docker compose up -d --build ui`.

Other UI commands, run with `pnpm --dir ui <script>`:

| Script | What it does |
|---|---|
| `test` | Vitest unit tests |
| `test:coverage` | Vitest with coverage and the thresholds of `ui/vitest.config.ts` |
| `lint` | ESLint and Stylelint |
| `typecheck` | TypeScript, no output |
| `format` | Prettier check (it does not rewrite files) |
| `build` | Production build |
| `gen:api` | Regenerates the API client `ui/src/api/schema.gen.ts` from `ui/openapi/openapi.json` |
| `gen:api:check` | Fails when the generated client is out of date |
| `ci:static` | `gen:api:check`, `format`, `lint`, `typecheck`, `test` and `build` in a row |

After a backend change that alters the API, regenerate the OpenAPI snapshot first with
`uv run python scripts/export_openapi.py`, then run `pnpm --dir ui gen:api`.

## Optional settings

| Variable | Default | Used for |
|---|---|---|
| `WAIT_TIMEOUT` | `300` | Seconds `run.sh` and `run.ps1` wait for the stack to become healthy |
| `E2E_WAIT_TIMEOUT` | `300` | The same for `scripts/e2e.sh` and `scripts/e2e-ui.sh` |
| `COMPOSE_PROJECT_NAME` | `contractum` (set by `name:` in `compose.yaml`, so the checkout folder name does not matter) | Running a second copy side by side (change port 8443 in `compose.yaml` too). Set it as an environment variable or with `-p`. The e2e runner follows it through `DC_E2E_COMPOSE_PROJECT` |
| `DC_E2E_DENIALS`, `DC_E2E_DENIAL_CONCURRENCY`, `DC_E2E_LOAD_*` | see `compose.test.yaml` | Sizes for the e2e load and denial tests |
| `DC_E2E_UID`, `DC_E2E_GID` | your user and group (`id -u`, `id -g`); 10001 when you run as root | The non-root user the `e2e` and `ui-e2e` runners run as, so they can write `.dev` and `ui/test-results` on a Linux host. Set by `scripts/e2e-runner-env.sh` |
| `DC_DOCKER_GID` | the group that owns the Docker socket inside a container | Added to the `e2e` runner only, for the tests that stop and start containers. Set by `scripts/e2e-runner-env.sh` |

A stack started before the project name was pinned runs under a project name taken from the folder.
The first `./run.sh` then starts a fresh stack (new volumes, secrets and admin password). Remove the
old one with `docker compose -p <old-project> down -v`.

To use a real Entra tenant instead of the mock, see [Entra setup](../admin/entra-setup.md).

## Mock identity provider and fake Graph

The compose stack ships a stand-in for Entra, so the stack, the e2e suites and the web UI's Entra
sign-in run without a tenant. It is a development convenience only. A real deployment uses none of
the following and configures a real tenant instead.

- **Mock issuer.** The `mock-oidc` container plays an Entra tenant with the issuer
  `https://localhost:8443/entra` and the tenant id `11111111-2222-4333-8444-555555555555`. Its
  discovery document is at `https://localhost:8443/entra/.well-known/openid-configuration`.
- **Dev router.** `gateway/dynamic/dev-mock-oidc.yml` routes `/entra/` to `mock-oidc`, so a browser
  can reach it. It is marked `DEV ONLY`, it is the only gateway file that names `mock-oidc` (a repo
  test pins this), and it strips no prefix: the mock builds `iss` and every endpoint from the
  `Host` and `X-Forwarded-Proto` headers and the issuer id in the path. Deleting the file removes
  the browser path and nothing else.
- **One issuer entry.** `DC_AUTH_ISSUERS` has exactly one `kind: entra` entry for the dev tenant.
  Its `issuer` is the browser-visible `https://localhost:8443/entra`, and its `jwks_uri` stays the
  in-network `http://mock-oidc:8080/entra/jwks`. A second entry for the same tenant stops every
  service from starting, because the issuer registry refuses a duplicate tenant.
- **Personas.** `alice` (member of the `data-eng` group), `bob` (no groups), `carol` (`data-eng`
  plus 250 generated groups, for the group overage case), `pipeline` (a service principal), and two
  bad tokens, `alice-idtoken` (no `scp`) and `alice-wrong-aud`.
- **Getting a persona token.** From inside the compose network (the e2e container, or
  `docker compose exec identity ...`), send a form `POST http://mock-oidc:8080/entra/token` with
  `grant_type=client_credentials`, `client_id=<persona>`, `client_secret=<anything>` and
  `scope=<persona>`. Send the headers `Host: localhost:8443` and `X-Forwarded-Proto: https` too.
  Without them `iss` is `http://mock-oidc:8080/entra` and every service refuses the token. The mock
  picks the persona's claims by `scope`, so a request without it gets a token with no persona
  claims.
- **Web UI personas.** The public clients `ui-alice` and `ui-bob` carry the same claims as `alice`
  and `bob`, picked by `client_id`, so the browser's authorization code and PKCE flow yields a token
  identity accepts. The compose `ui` service sets `DC_UI_ENTRA_CLIENT_ID=ui-alice`,
  `DC_UI_ENTRA_AUTHORITY=https://localhost:8443/entra` and `DC_UI_ENTRA_PROTOCOL=oidc`. All of these
  are public identifiers.
- **Fake Graph.** Identity's real Graph client talks to the compose-only `fake-graph` service
  (backend network only, no published port). Its seed is `infra/fake-graph/directory.json`. Tests
  change it through `POST http://fake-graph:8080/_fake/principals/{oid}` and
  `POST /_fake/groups/{id}`, and restore the seed with `POST /_fake/reset`. Always reset afterwards,
  because every e2e test shares the state. Identity caches the directory for 60 seconds, so a
  disabled or reset principal is seen after up to a minute.

After editing `infra/mock-oidc/config.json`, run `docker compose restart mock-oidc`. The file is
read at start, and `docker compose up -d` does not restart the container for a changed bind mount.

## Mail catcher and webhook sink

The `mail` service sends notices to `mailpit`, a compose-only mail catcher. Subscriber card
webhooks go to `webhook-sink`, a compose-only receiver that can be told to fail the next
deliveries. Both live on the backend network only: no published port and no gateway route. From the
e2e container they are `http://mailpit:8025` and `http://webhook-sink:8080`. After any manual use,
reset them (`DELETE /api/v1/messages` on Mailpit, `POST /_sink/reset` on the sink).

Notices are asynchronous (outbox, NATS, the contracts consumer, then the delivery worker), so they
arrive a moment after the lifecycle call.

## Logs, health and secrets

- `docker compose ps` shows every container and its health. `docker compose logs -f <service>`
  follows one service. The services log JSON, and `Authorization` is scrubbed.
- Each service reports `/healthz` (liveness) and `/readyz` (database reachable) inside the compose
  network only. The gateway never routes them.
- The secrets live in the `dev-secrets` volume, one folder per consumer, mounted read-only at
  `/run/secrets` in that consumer only. Only `.dev/ca.crt`, `.dev/bootstrap_admin_secret` and, after
  the smoke test, `.dev/admin_password` reach the host.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `run.sh` stops before starting | It prints what is missing: Docker not running, Compose older than 2.21, or port 8443 busy |
| A container stays "starting" or "unhealthy" | `docker compose ps`, then `docker compose logs <service>` |
| A `*-migrate` job failed | `docker compose logs <service>-migrate`. The service does not start until its migration succeeds |
| `dev-init` failed with "CA state inconsistent" | One of the CA files is missing. Reset with `./run.sh reset` (or `docker compose down -v`) |
| Browser or curl TLS error | Trust `.dev/ca.crt`, or use `curl --cacert .dev/ca.crt` |
| Admin sign-in fails after a reset | The secrets were regenerated, so the old `.dev/admin_password` no longer works. `./run.sh smoke` runs the bootstrap again with the new `.dev/bootstrap_admin_secret` |
| Every request answers a 404 problem | The path is not routed. Only `/api/v1/<module>/...`, the docs paths and the web UI are routed |
| 502 or 503 problem from the gateway | The target service is down or not healthy yet: `docker compose ps`, `docker compose logs <service>` |
| 429 `too-many-requests` | The gateway's rate limit (50 per second, 5 per second on `/api/v1/identity/auth/`). Slow down |
| `/api/v1/openapi.json` answers 503 | `docker compose logs api-docs`: a merge collision names both services. Fix it by renaming the model in one service |
| The API docs miss a change | `api-docs` merges the documents once at start. Run `docker compose restart api-docs` |
| Publish answers 503 `validation-unavailable` | `docker compose logs validation` (look for `rule_worker_not_ready` and OOM kills), then `docker compose logs contracts` (look for `validation_unreachable`, `validation_bad_status`, `validation_bad_answer` or `validation_hash_mismatch`) |
| An audit event never appears | Check that NATS is healthy, then the producer's logs for `outbox_relay_publish_failed` |
| Access events are missing | Expected while NATS has trouble: look for `access_events_dropped` in the contracts logs |
| NATS permission errors after editing `infra/nats/nats.conf.tmpl` | Run `docker compose up -d` again: `dev-init` re-renders `nats.conf`. A stream change other than the `ACCESS` limits needs `docker compose down -v` |
| Notices (email, webhook) do not arrive | They are asynchronous. Check Mailpit and the webhook sink from the backend network |

## Reset and tear down

- `./run.sh down` (or `docker compose down --remove-orphans`) stops the stack and keeps the data.
- `./run.sh reset` deletes the containers, the `dev-secrets`, `pgdata` and `natsdata` volumes, and
  `.dev/`. The next start generates every secret again, including a new bootstrap secret.
- The smoke test and the e2e fixtures run the bootstrap again on their own after a reset.
