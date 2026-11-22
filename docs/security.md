# Security

Contractum holds the contracts that data pipelines depend on, so security is part of how it is
built, tested and released. It is not a step added at the end. This page gives the overall picture.
The linked pages have the details.

## Built in

- **Every request is authenticated and authorized by the service itself.** The gateway can be
  replaced, so no security control lives only there. Access follows the folder tree (Root,
  Organization, Project, Team) and is checked on every call.
- **Sign-in:** Microsoft Entra ID or local accounts. Local passwords are hashed with Argon2id.
  Personal access tokens and service account tokens are stored only as hashes and can be revoked.
- **No user code runs on the server.** Validation rules are declarative: CEL conditions with
  JSONPath targets, with limits on size and evaluation cost. A mandatory rule that cannot be
  evaluated blocks publishing, so it fails closed.
- **Everything is audited:** every change, read and refusal, with who did it and when.
- **Hardened deployment:** the Helm chart runs every container with a read-only root file system
  and no privilege escalation, and the application containers as non-root (the exceptions are in
  [Deploy on Kubernetes](admin/deploy-kubernetes.md#security-notes)). It also ships network
  policies and serves HTTPS at the gateway.

## Checked on every change

Every pull request and every push to `main` runs these checks. Most of them fail the build on a
finding. The rest report to GitHub code scanning for review.

| Area | Tools |
|------|-------|
| Code analysis | CodeQL (Python, TypeScript, security-extended queries), ruff security rules (Bandit), mypy strict typing |
| Code quality and coverage | SonarQube release quality gate: no new issues, little duplication, test coverage of at least 98% from real tests |
| Dependencies | Dependabot (security and version updates), GitHub dependency review, pip-audit, pnpm audit, Trivy |
| Container images and config | Trivy scans of all service images and the CLI image, Dockerfiles, Compose files and the Helm chart |
| Secrets | gitleaks over the full git history, GitHub secret scanning with push protection |
| Supply chain | Every GitHub Action is pinned to a commit SHA. Scanners are official releases with verified checksums. SDK releases carry SHA-256 checksums and build provenance attestations |

## Tested against the running system

Every night when `main` has changed, the whole platform is started and attacked from the outside:

- **OWASP ZAP:** a passive scan of the web UI and an active scan of the full API.
- **Schemathesis:** fuzzes every API operation and fails on server errors or responses that do not
  match the published OpenAPI document.
- **End-to-end tests:** test users with different roles, including checks that people without
  access are refused.

## Reviewed against a standard

Contractum is reviewed against **OWASP ASVS 5.0 Level 2**: all 253 level 1 and 2 requirements,
each with a status and evidence in the code. Open gaps are listed openly with the reason, not
hidden.

## Every exception has a reason

A scanner finding is fixed, or it goes on an ignore list with a written reason and, where possible,
an expiry date. Nothing is silenced without an explanation.
