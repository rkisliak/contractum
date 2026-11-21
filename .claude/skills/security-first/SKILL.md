---
name: security-first
description: Security is a top priority for every change. Before finishing any change, check dependencies (CVEs), secrets, static analysis, SonarQube and test coverage, fix the root cause of every finding (never hide it with exclusions, ignores or pragmas), and ask the human whenever a security question is not clear.
---

# security-first

Contractum is the place data pipelines trust for governed contracts. Security is one of the top
priorities. It is **never** traded for speed and **never** made to look better than it is.
This skill applies to every change: code, dependencies, images, Helm, CI, docs that describe
security.

## 1. Rules (mandatory)

1. **Fix the root cause.** A finding is fixed in the code, the dependency or the configuration
   that causes it.
2. **Never hide a finding.** Do not add or widen:
   - SonarQube exclusions (`sonar.exclusions`, `sonar.coverage.exclusions`, `sonar.cpd.exclusions`,
     `sonar.issue.ignore.*`), or mark issues and hotspots "won't fix" or "safe" to pass the gate;
   - `# noqa`, `# type: ignore`, `# nosec`, `# pragma: no cover`, `eslint-disable`, `@ts-ignore`;
   - entries in `.trivyignore`, `.pip-audit-ignore`, `.gitleaks.toml` allowlists or
     `.zap/rules.tsv`;
   - coverage `omit`/exclude settings, or lower coverage thresholds.
   The only exception is a proven false positive or a finding that is provably not exploitable
   here. Then **ask the human first**. Once they agree, add the narrowest possible entry, with a
   written reason (and an expiry where the format allows one).
3. **Maximum coverage from real tests.** Whole-project coverage stays at **98% or more** (the
   "Contractum release" SonarQube gate and `scripts/coverage_gate.py`), from tests that check
   behaviour. New code aims for 100%. When you report coverage, say what it was measured from
   (unit only, or unit plus integration combined).
4. **Zero open issues.** No new SonarQube issues or hotspots, no known vulnerabilities in any
   lock file or image that a fix exists for, no secrets in the code or the git history.
5. **Ask when unsure.** If a security question is not clear (a risk to accept, a control to
   weaken, a finding that might be a false positive, a permission or auth change, a product rule
   that affects access, data exposure or crypto), stop and ask the human to judge. Do not decide
   it alone and do not "decide safely and document it" for security.
6. **Never weaken a control to make something work.** This covers auth checks, permission gates,
   TLS, token lifetimes, rate limits, input limits, CSP and security headers, non-root containers
   and network policies. If a control blocks you, ask.
7. **Secrets:** never commit, print, log or paste a secret (tokens, passwords, keys, client
   secrets), including in reports and summaries. A leaked real secret is rotated, never
   allowlisted.

## 2. What to check, by change

| You changed | Check |
|-------------|-------|
| Python code (any service, shared libs, `clients/python`) | `ruff check` (includes the Bandit `S` rules), `mypy`, unit and affected integration tests, coverage, SonarQube |
| UI code | `pnpm ci:static` (lint, types, tests, build), `pnpm --dir ui audit --prod --audit-level high`, SonarQube |
| A Python dependency or `uv.lock` | pip-audit on **both** locks (server workspace and `clients/python/uv.lock`), Trivy filesystem |
| A UI dependency or `pnpm-lock.yaml` | `pnpm audit`, Trivy filesystem |
| A Dockerfile, base image or the CLI image | Trivy image scan of every affected image (the 7 service images and `contractum-cli`) |
| Helm chart, compose, Dockerfiles | Trivy misconfiguration scan; keep non-root, read-only root file system, no privilege escalation, network policies |
| Auth, permissions, tokens, sessions, input handling | the authz golden matrix and route-policy walker tests, the integration tests for refused callers, and ask the human about any behaviour change |
| Anything touching the API surface | Schemathesis and OWASP ZAP on the compose test stack (never against the Kubernetes deployment) |
| Any commit | gitleaks over the history; no secret, home path or personal data in tracked files |

The exact commands, versions and the ignore-list formats are in
[`docs/dev/ci-and-github.md`](../../../docs/dev/ci-and-github.md), sections "Security scanners",
"Ignore lists" and "Running the scanners locally". CI runs the same tools in
`.github/workflows/security.yml`, `ci.yml`, `sdk.yml` and `dast-nightly.yml`.

## 3. SonarQube

- Local server: SonarQube at `http://127.0.0.1:9000`, project `contractum`, quality gate
  "Contractum release" (coverage of at least 98%, no new issues, low duplication).
- Feed it **combined** unit and integration coverage, never unit-only numbers.
- A failing gate is fixed in the code or the tests. Changing the gate, its conditions or the
  project's exclusions needs the human's explicit approval.

## 4. When a finding cannot be fixed now

For example: a CVE in a base image with no upstream fix yet, or a design gap that needs a product
decision.

1. Tell the human: the finding, the real risk here, and the options.
2. Record the agreed decision in the dated audit report in the private notes repository, and keep the
   OWASP ASVS review (`docs/admin/security/asvs-l2.md`) honest (Met, Partial or Not met).
3. Re-check on the next change: a fix may have appeared.

## 5. Reporting

When you finish, report the security checks you ran and their results plainly: tool, scope and
result. Say what was **not** checked, and why. Never report "all good" for a scan you did not run.
