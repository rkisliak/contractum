# CI and GitHub

This page covers the GitHub Actions workflows, the security scanners and their ignore lists, and
every repository setting to configure once after the repository exists and the first push has
happened.

What the workflows publish:

- `sdk-release.yml` publishes the SDK and CLI, but only on an `contractum-v*` tag (see
  [The SDK release](#the-sdk-release)).
- `docs.yml` deploys the docs site to GitHub Pages, but only on a push to `main` when the
  repository variable `DOCS_PAGES_ENABLED` is `true`.
- Every other workflow only builds, tests and scans. Server images are built but never pushed, and
  nothing is deployed.

The only repository secret any workflow reads is `SONAR_TOKEN`, in the inactive `sonarqube` job.
A few jobs use the automatic `GITHUB_TOKEN`: the SDK release (to create the release and push the
CLI image), the weekly link check (to raise GitHub's rate limit), and the `gate` jobs of
`e2e-nightly` and `dast-nightly` (to find the last green run and skip a nightly run when nothing
changed).

## The workflows

| Workflow | Trigger | Jobs | What it checks or publishes |
|----------|---------|------|-----------------------------|
| `ci` (`ci.yml`) | pull request, push to `main` | `python-lint`, `python-unit`, `python-integration`, `ui`, `docker-build (<image>)`, `ci-ok` | The job-level mirror of `scripts/ci.sh` without the end-to-end step: lock check, ruff, mypy and import rules; unit tests; integration tests against the runner's Docker; the UI static checks (`pnpm ci:static`); every image builds (identity, contracts, validation, audit, mail, api-docs, ui), never pushed. `ci-ok` is one aggregate result |
| `security` (`security.yml`) | pull request, push to `main`, daily 04:23 UTC, manual | `trivy-fs-report`, `trivy-fs-gate`, `trivy-image (<image>)`, `dependency-review`, `codeql (<language>)`, `gitleaks`, `pip-audit`, `pnpm-audit`, `sonarqube` | The scanners in [Security scanners](#security-scanners). `dependency-review` runs on pull requests only. `sonarqube` is inactive until `SONAR_ENABLED` is `true` |
| `e2e-nightly` (`e2e-nightly.yml`) | nightly 01:41 UTC, manual (input `full`) | `gate`, `e2e` | The cold-start end-to-end run of `scripts/ci.sh`, or the full gate when `full` is ticked |
| `dast-nightly` (`dast-nightly.yml`) | nightly 03:17 UTC, manual | `gate`, `dast` | Starts the compose test stack, runs Schemathesis over the merged OpenAPI document and OWASP ZAP (baseline and API scan). Fails on a Schemathesis failure or an unignored High alert |
| `sdk` (`sdk.yml`) | pull request and push to `main` that touch `clients/python/**`, `ui/openapi/**`, the SDK docs or the workflow; nightly 02:47 UTC; manual | `sdk-lint`, `sdk-unit (python <version>)`, `sdk-os (<os>)`, `sdk-lowest`, `sdk-latest`, `sdk-unit-coverage`, `sdk-integration`, `sdk-ok` | The SDK and CLI in `clients/python`: lock, ruff, mypy, import rules, the generated client and the CLI and SDK reference pages are up to date; unit tests on Python 3.11 to 3.14, on Windows and macOS, with the lowest and the newest allowed dependencies; a 98 percent unit-only coverage floor; the integration suite against the real compose stack with a 98 percent combined coverage floor. `sdk-ok` is one aggregate result |
| `sdk-release` (`sdk-release.yml`) | pull request that touches `clients/python/**` or the workflow; push of an `contractum-v*` tag; manual | `build-and-smoke`, `github-release`, `image` | Every run builds the wheel and sdist, installs each on Python 3.11 and 3.14, checks the Apache-2.0 licence files, writes `SHA256SUMS`, builds the CLI image and runs an offline smoke on it. Only an `contractum-v*` tag runs `github-release` and `image`, which publish |
| `docs` (`docs.yml`) | pull request, push to `main`, manual | `docs-lint`, `docs-build`, `pages-deploy` | markdownlint, typos and an offline lychee check of local links, images and heading anchors; the HTTP API reference drift check; `zensical build --strict`. `pages-deploy` deploys the site only on a push to `main` with `DOCS_PAGES_ENABLED` set to `true` |
| `docs-links-weekly` (`docs-links-weekly.yml`) | Mondays 05:17 UTC, manual | `external-links` | lychee over the external links of every Markdown file. It never runs on pull requests, so a remote site that is down never blocks a merge |

The nightly `e2e-nightly` and `dast-nightly` runs skip a scheduled run when the head of `main`
equals the head of the last green run. A manual run always runs.

### The SDK release

Push a tag `contractum-vX.Y.Z` that equals the project version in `clients/python/pyproject.toml` (for
example `contractum-v0.1.0` for version 0.1.0). The run fails on a mismatch. On the tag:

1. `build-and-smoke` builds and tests the files, as on a pull request.
2. `github-release` creates the GitHub Release of the tag with the GitHub CLI. It carries the
   wheel, the sdist and `SHA256SUMS`, checked against the files `build-and-smoke` tested. It adds
   GitHub artifact attestations (build provenance) on a public repository, or on a private one when
   the repository variable `SDK_ATTESTATIONS` is `true` (attestations on a private repository need
   GitHub Enterprise Cloud). A version with `a`, `b`, `rc` or `dev` in it is marked as a
   pre-release.
3. `image` builds the CLI image and pushes it to `ghcr.io/<owner>/contractum-cli`, tagged with the
   version and `latest`. This job is the only one that holds `packages: write`.

Nothing goes to a package index. Users install the wheel from its release URL; see the
[SDK and CLI guide](../user/sdk-cli.md).

### The nightly end-to-end run

- Run it by hand from Actions, e2e-nightly, Run workflow, on `main`. Tick `full` for the full gate,
  or use `gh workflow run e2e-nightly.yml -f full=true`.
- It needs no secrets. It needs Docker on `ubuntu-latest`, a free port 8443, the disk space its
  first step frees, and up to 150 minutes (default run) or 340 minutes (full gate).
- On failure, the `e2e-failure-<run id>` artifact holds the compose logs, the Playwright traces and
  reports and `.coverage-data`. It is kept for 7 days. `.dev` is never uploaded.
- Artifacts of a public repository can be downloaded by any signed-in GitHub user. This one holds
  only throwaway values of a stack that has been destroyed.

### The nightly DAST run

- Run it by hand from Actions, dast-nightly, Run workflow. It needs no secrets and the same runner
  resources as the nightly end-to-end run.
- It fails on a Schemathesis failure and on any High ZAP alert that `.zap/rules.tsv` does not
  ignore with a reason. ZAP still runs after a Schemathesis failure, so one night reports both.
- The `dast-reports-<run id>` artifact (14 days) holds the Schemathesis log and the ZAP HTML and
  JSON reports.

## Repository settings and variables

| Name | Kind | Used by | Effect |
|------|------|---------|--------|
| `SONAR_ENABLED` | variable | `security.yml` `sonarqube` | `true` turns the SonarQube Cloud job on. Unset: the job is skipped |
| `SONAR_ORGANIZATION`, `SONAR_PROJECT_KEY` | variables | `security.yml` `sonarqube` | The SonarQube Cloud organization and project keys |
| `SONAR_TOKEN` | secret | `security.yml` `sonarqube` | The SonarQube Cloud token. The only repository secret any workflow reads |
| `DOCS_PAGES_ENABLED` | variable | `docs.yml` `pages-deploy` | `true` deploys the docs site to GitHub Pages on every push to `main` |
| `SDK_ATTESTATIONS` | variable | `sdk-release.yml` `github-release` | `true` adds build attestations on a private repository. A public repository always gets them |

### Docs site and GitHub Pages

1. Settings, Pages: set the source to **GitHub Actions**.
2. Set the repository variable `DOCS_PAGES_ENABLED` to `true`.
3. The next push to `main` builds and deploys the site from the `pages-deploy` job, in the
   `github-pages` environment. That job is the only one that holds `pages: write` and
   `id-token: write`.

`site_url` in `mkdocs.yml` defaults to the GitHub Pages address
(`https://rkisliak.github.io/contractum/`). It reads the environment variable `DOCS_SITE_URL` first. The deploy job sets that
variable from the Pages URL that `actions/configure-pages` returns, so a deployed site always has
the right URL. For a local build with a real URL, set `DOCS_SITE_URL` yourself.

## Branch protection on `main`

Settings, Rules, Rulesets (or classic branch protection):

- Require a pull request before merging. Block force pushes. Block deletion.
- Require these status checks: `ci-ok`, `trivy-fs-gate` and `dependency-review`. `ci-ok` rolls up
  `python-lint`, `python-unit`, `python-integration`, `ui` and every `docker-build` job, so a change
  to the image matrix never orphans a required check.
- `docs-lint` and `docs-build` run on every pull request and are good candidates to require.
- `sdk-ok` rolls up every `sdk.yml` job. `sdk.yml` runs only when its paths change, so a required
  `sdk-ok` would wait forever on a pull request that does not touch them. Require it only together
  with a rule that handles skipped workflows.
- Add the rule "Require code scanning results" for CodeQL, with security alerts of High or higher
  and alerts of Errors. Trivy may be added as optional.
- `gitleaks`, `pip-audit` and `pnpm-audit` are good candidates to require once they have run green
  on `main`.
- Check names appear in the picker only after each workflow has run once.

### Code owners and the pull request template

- `.github/CODEOWNERS` makes the repository owner (see `.github/CODEOWNERS`) the owner of every
  Markdown file, `docs/`, `mkdocs.yml`, the three docs lint configs and the docs workflows. With
  "Require review from Code Owners" on, those changes need that review.
- `.github/pull_request_template.md` asks for a summary, whether the docs were updated (or why they
  are not needed), and which checks were run.

## Code security settings

Settings, Advanced Security (Code security):

- Dependency graph: on. Check that both `uv.lock` and `ui/pnpm-lock.yaml` appear in the graph,
  because dependency review only sees what the graph parses.
- Dependabot alerts: on. Dependabot security updates: on.
- Grouped security updates: leave it **off**, so security fixes stay one pull request each. This
  matches `.github/dependabot.yml`, which groups only version updates.
- Secret scanning: on. Push protection: on.
- Code scanning uses the advanced setup through `security.yml`. Do **not** also enable the default
  setup: GitHub rejects advanced CodeQL uploads while the default setup is on.
- Private vulnerability reporting: on.
- Copilot Autofix may stay on.

A public repository needs no paid GitHub Advanced Security: code scanning, CodeQL, secret scanning,
push protection and dependency review are free there. On a private repository, code scanning,
CodeQL, dependency review and Copilot Autofix need GitHub Code Security, and secret scanning, push
protection and validity checks need GitHub Secret Protection. Both are billed per active committer.

## Actions settings

Settings, Actions, General:

- Workflow permissions: read repository contents. Do not allow Actions to create or approve pull
  requests. Jobs that need more (the SDK release, the Pages deploy) ask for it in their own
  `permissions:` block.
- Fork pull requests: require approval for first-time contributors.
- Allowed actions: GitHub-owned actions, plus exactly `astral-sh/setup-uv`, `pnpm/action-setup`,
  `docker/setup-buildx-action`, `docker/build-push-action` and `SonarSource/sonarqube-scan-action`
  (the last one only matters once SonarQube Cloud is activated).
- Enable "Require actions to be pinned to a full-length commit SHA". Every `uses:` in the
  workflows is already pinned this way, with the version as a comment.
- Artifact and log retention: short, for example 14 days.

## Dependabot

- Cooldown: 3 days for uv, docker, docker-compose, helm and github-actions. 14 days for npm,
  because `ui/pnpm-workspace.yaml` sets `minimumReleaseAge: 20160` (14 days) and pnpm refuses
  anything younger. The cooldown never applies to security updates.
- Grouping: one weekly pull request per ecosystem for minor and patch version updates. No group
  applies to security updates. `fastapi`, `jsonschema-rs` and `cel-expr-python` are exact pins and
  always get their own pull request.
- Paired pins that need a hand-made companion change:
  - the `mcr.microsoft.com/playwright` image must equal `@playwright/test`
    (`tests/repo/test_compose_topology.py` enforces it),
  - the `node` image must equal `ui/.nvmrc` and `engines.node` in `ui/package.json`.
- Python minor versions of the `python` base image are ignored for `/clients/python` (the SDK
  tests Python 3.11 to 3.14) and `/infra/docker` (the server workspace requires Python below
  3.15). Patch updates still come. A new Python minor is a deliberate upgrade made by hand.
- Nothing merges automatically.

## Security scanners

Every scanner is an official CLI or image, run directly, pinned and verified. No third-party
wrapper action wraps a scanner.

| Tool | What it covers | Workflow and job | Gate | Results |
|------|----------------|------------------|------|---------|
| Trivy filesystem (report) | Vulnerabilities in `uv.lock` and `ui/pnpm-lock.yaml`, secrets, misconfiguration in Dockerfiles, compose and Helm | `security.yml`, `trivy-fs-report` | none (report) | code scanning, category `trivy-fs` |
| Trivy filesystem (gate) | Fixable CRITICAL and HIGH vulnerabilities, secrets | `security.yml`, `trivy-fs-gate` | fails on any finding | job log |
| Trivy image | Vulnerabilities and secrets in the 7 service images and the `contractum-cli` image | `security.yml`, `trivy-image (<image>)` | none (report) | code scanning, category `trivy-image-<image>` |
| dependency review | New dependencies with advisories | `security.yml`, `dependency-review` (pull requests) | fails on high or above | job log |
| CodeQL | Python and JavaScript/TypeScript, `security-extended` queries | `security.yml`, `codeql (<language>)` | code scanning rule | code scanning |
| gitleaks | Secrets in the full git history | `security.yml`, `gitleaks` | fails on any finding | code scanning, category `gitleaks` (redacted) |
| pip-audit | Known vulnerabilities in the locked Python dependencies: the server workspace `uv.lock` and the `contractum` package's `clients/python/uv.lock`, each audited separately | `security.yml`, `pip-audit` | fails on any vulnerability not in `.pip-audit-ignore` | job log |
| pnpm audit | Known vulnerabilities in the UI production dependencies | `security.yml`, `pnpm-audit` | fails on high or critical | job log |
| SonarQube Cloud | Code quality, security hotspots, coverage | `security.yml`, `sonarqube`, **only when `SONAR_ENABLED` is `true`** | the SonarQube quality gate (optional required check) | SonarQube Cloud |
| Schemathesis | Every operation of the merged OpenAPI document, fuzzed as the Global admin: server errors and response conformance | `dast-nightly.yml`, `dast` | fails on any failing check | `schemathesis.log` in the artifact |
| OWASP ZAP baseline | Passive scan of the UI | `dast-nightly.yml`, `dast` | `scripts/zap_gate.py`: any High alert not ignored | `zap-baseline.html` and `.json` in the artifact |
| OWASP ZAP API scan | Active scan of the merged OpenAPI document with the admin token | `dast-nightly.yml`, `dast` | same | `zap-api.html` and `.json` in the artifact |
| ruff `S` rules | Bandit checks on every Python file | `ci.yml`, `python-lint` | fails on any finding | job log |

Trivy misconfiguration findings are reported but not gated. They include deliberate dev-only
choices, such as root test-runner images. Triage or dismiss them in code scanning.

### Ignore lists

Every ignore list follows one policy: **a reason for every entry**, and an expiry wherever the
format allows one. An ignore is for a false positive, or for a finding that is provably not
exploitable here, never for "fix later". Prefer fixing over ignoring.

| File | Used by | Entry format | Policy |
|------|---------|--------------|--------|
| `.trivyignore` | Trivy | `CVE-... exp:YYYY-MM-DD`, with a comment above giving the reason, the owner, the advisory link and why it is not exploitable | ships empty |
| `.pip-audit-ignore` | pip-audit | `GHSA-... exp:YYYY-MM-DD`, with the same comment block. The job fails on an entry without an expiry and on an expired entry | ships empty |
| `.gitleaks.toml` | gitleaks | One `[[allowlists]]` entry per proven test fixture or documentation sample, with a `# reason:` comment, a `description`, a path (and the line shape where the file has real code) and one rule. Never a secret value | fake values only. A real secret is rotated, never allowlisted |
| `.zap/rules.tsv` | ZAP and `scripts/zap_gate.py` | `<plugin id> <TAB> IGNORE <TAB> (<reason>)` for a whole alert id, or `<plugin id> <TAB> OUTOFSCOPE <TAB> <URL regex>` per URL. A `# reason:` comment covers the OUTOFSCOPE rows after it in the paragraph, and a `# params: a, b` comment directly above a row lists the parameter names ZAP reports. `zap_gate.py` exits 2 on a row without a reason | false positives only, narrowed to the proven URLs and parameters |

`scripts/zap_gate.py` is the authoritative decision: ZAP's JSON report keeps every alert, so the
gate re-applies the rows per instance (plugin, URL and parameter), and one instance outside them
fails the run. A real finding stays visible, and is fixed or recorded as a decision in the dated
audit report in the maintainers' private notes.

### Upgrading the pins

| Pin | Where | How to upgrade |
|-----|-------|----------------|
| Trivy | `TRIVY_VERSION`, `TRIVY_SHA256` in `security.yml` | Take both from the release's `trivy_<version>_checksums.txt` (the `Linux-64bit.tar.gz` line) and change them together. Never use 0.69.4, 0.69.5 or 0.69.6, the builds compromised in the March 2026 supply-chain incident |
| gitleaks | `GITLEAKS_VERSION`, `GITLEAKS_SHA256` in `security.yml` | Take both from the release's `gitleaks_<version>_checksums.txt` (the `linux_x64.tar.gz` line). Re-run the full-history scan locally: new default rules can raise new findings |
| pip-audit | `PIP_AUDIT_VERSION` in `security.yml` | The latest release on PyPI. It runs through `uvx`, outside the workspace lock |
| ZAP image | `ZAP_IMAGE` in `scripts/zap-scan.sh` | `docker buildx imagetools inspect ghcr.io/zaproxy/zaproxy:stable`, copy the top-level digest, then run the scan locally and review the new alerts |
| lychee | `LYCHEE_VERSION`, `LYCHEE_SHA256` in `docs.yml` and `docs-links-weekly.yml`; the image digest in the `lychee.toml` header | Take both from the `lychee-v<version>` release and keep the two workflows equal |
| markdownlint-cli2 | the image tag and digest in `docs.yml` | Pick the new tag and its index digest (`docker buildx imagetools inspect`) and change them together |
| Actions | `uses:` lines | Pin the full 40-character commit SHA with the version as a comment. For an annotated tag, `gh api repos/OWNER/REPO/commits/TAG --jq .sha` |
| Schemathesis, zensical, typos | `pyproject.toml` (exact pins) | With the rest of the lock, through Dependabot or by hand |

### Running the scanners locally

Run from the repository root. Scan outputs go to `.security-reports/`, which is git-ignored: never
commit a report, and never paste a secret from one. The stack-based scans (Schemathesis, ZAP) run
**only against the compose test stack**, never against a Kubernetes deployment. The compose stack
and a Kubernetes gateway cannot hold host port 8443 at the same time.

| Tool | Command |
|------|---------|
| gitleaks (full history) | `gitleaks git --config .gitleaks.toml --redact --no-banner --log-opts="--all" --report-format sarif --report-path .security-reports/gitleaks.sarif .` |
| pip-audit | `uv export --frozen --all-packages --all-groups --no-hashes --no-emit-workspace -o .security-reports/requirements-audit.txt`, then `uvx pip-audit==<PIP_AUDIT_VERSION> -r .security-reports/requirements-audit.txt --strict --no-deps --disable-pip --desc`. Repeat for the package: `cd clients/python && uv export --frozen --all-groups --no-hashes --no-emit-project -o ../../.security-reports/requirements-audit-cli.txt` and audit that file the same way |
| pnpm audit | `pnpm --dir ui audit --prod --audit-level high` |
| Trivy filesystem | Scan a clean export so `.venv` and `node_modules` are skipped: `git archive HEAD \| tar -x -C <empty dir>`, then `trivy fs --scanners vuln,secret,misconfig --ignorefile .trivyignore <dir>` and the gate command from `security.yml` |
| Trivy image | `trivy image --scanners vuln,secret --ignore-unfixed contractum/<service>:dev`; for the CLI, build `docker build -t contractum-cli:scan clients/python` and scan `contractum-cli:scan` |
| CodeQL | The official CodeQL bundle: `codeql database create <db> --language=<python\|javascript-typescript> --build-mode=none --source-root <clean export>`, then `codeql database analyze <db> <language>-security-extended.qls --format=sarif-latest --output=<file>.sarif` |
| Schemathesis | `docker compose up -d --build --wait` and `bash scripts/skeleton-smoke.sh`, then `bash scripts/e2e.sh tests/e2e/test_phase7_schemathesis.py` |
| ZAP | With the same stack up: `bash scripts/zap-scan.sh .security-reports/zap` (reports and the High gate) |
| ZAP gate only | `python3 scripts/zap_gate.py .security-reports/zap/zap-baseline.json .security-reports/zap/zap-api.json --rules .zap/rules.tsv` |

Tear the stack down afterwards:
`docker compose -f compose.yaml -f compose.test.yaml --profile test down -v`.

The ZAP API scan is an active scan. It sends attack traffic carrying the admin token to the test
stack, creates and deletes data there, and trips the gateway rate limiter (429 responses are
expected noise, not findings). The active scan is capped below the 15 minute lifetime of the access
token. ZAP runs last on purpose, because the scan feeds the login throttle.

## Docs checks locally

The same checks as `docs.yml`, from the repository root:

| Check | Command |
|-------|---------|
| markdownlint | `docker run --rm -v "$PWD:/workdir:ro" davidanson/markdownlint-cli2:v0.24.2@sha256:c768e94d8bb161c449bde28f60656929ca1f02c3010a405f65c9cac71563a889` (config `.markdownlint-cli2.jsonc`) |
| typos | `uv run --group docs typos` (config `_typos.toml`) |
| lychee (offline) | `docker run --rm -v "$PWD:/repo:ro" -w /repo lycheeverse/lychee:0.24.2@sha256:e2d19e57cf6ab037026f20b8e449a1f30d9d7f81eef4194763aab2eab20bd28d --offline --config lychee.toml './**/*.md'` |
| HTTP API reference | `uv run --group docs python scripts/gen_http_reference.py --check` |
| CLI and SDK reference | `cd clients/python && uv run python scripts/gen_reference.py --check` |
| Site build | `uv run --group docs zensical build --strict`, or `uv run --group docs zensical serve` to preview |

Use `uv run --group docs ...` locally. `uv sync --only-group docs` would remove the development
packages from your environment.

## SonarQube Cloud activation

The `sonarqube` job and `sonar-project.properties` are prepared and inactive. After buying
SonarQube Cloud:

1. Create the organization on <https://sonarcloud.io> by importing the GitHub organization or
   account that owns the repository.
2. Create the project from the repository, choose **GitHub Actions** as the analysis method and
   turn **Automatic Analysis off** (the job already scans, and both together are rejected).
3. Generate a token (My Account, Security) and add it as the repository secret `SONAR_TOKEN`
   (Settings, Secrets and variables, Actions, Secrets).
4. Add the repository variables `SONAR_ORGANIZATION` (the organization key) and
   `SONAR_PROJECT_KEY` (the project key) on the same page, under Variables.
5. Set the repository variable `SONAR_ENABLED` to `true`. The job then runs on pull requests (not
   from forks), on `main` and nightly. Check the first run: the coverage paths in
   `sonar-project.properties` must resolve to source files, or the coverage column stays empty.
6. Optionally add the SonarQube quality gate as a required status check in the `main` ruleset.

The job reads `SONAR_TOKEN` and nothing else. Until step 5, it is skipped on every run.

### Coverage reports for SonarQube

The job produces three coverage reports before the scan, and a local scan needs the same files
first. None of them is committed.

- **Server (Python):** unit plus integration tests under coverage, from the repository root. The
  integration tests need a Docker daemon for testcontainers.

  ```sh
  uv run pytest -m "unit and not timing and not performance" --cov --cov-report=
  uv run pytest -m "integration and not timing and not performance" --cov --cov-append --cov-report=
  uv run coverage xml -o coverage.xml
  ```

  This is the release coverage. The unit run alone leaves routes, units of work and repositories
  (exercised by the integration tests) uncovered. `timing` tests assert CPU or wall-clock budgets
  and run untraced, as in the full gate. `[tool.coverage.run]` in `pyproject.toml` names the
  packages and sets `relative_files = true`, so the XML holds paths such as
  `identity/src/dc_identity/app.py` that resolve from the scanner's base directory.
- **SDK (Python):** with the compose test stack up and smoke-tested,
  `bash clients/python/scripts/combined_coverage.sh --require-integration` writes
  `clients/python/coverage.xml` (unit plus integration, combined).
- **UI:** `pnpm --dir ui exec vitest run --coverage`. `ui/vitest.config.ts` adds an `lcovonly`
  reporter with the repository root as `projectRoot`, so `ui/coverage/lcov.info` names files as
  `ui/src/...`. In a container, mount the whole repository (not only `ui/`), or the paths are
  written relative to the container root. With the repository mounted, keep the pnpm store out of
  the checkout (`pnpm install --store-dir <path inside the node_modules volume>`), or pnpm creates
  `.pnpm-store/` at the repository root.

The project's quality gate "Contractum release" fails below 98 percent overall coverage. The
measured numbers are in the dated SonarQube report in the maintainers' private notes.

## Security standard and reports

- The [OWASP ASVS 5.0 Level 2 review](../admin/security/asvs-l2.md) lists every level 1 and 2
  requirement with a status and evidence.
- The dated audit and SonarQube reports are kept in the maintainers' private notes, not in this
  repository. The raw scan outputs of a run stay in the git-ignored `.security-reports/` and are
  never committed.
