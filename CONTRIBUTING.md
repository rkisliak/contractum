# Contributing to Contractum

Thank you for your interest in Contractum. This page explains how the repository works today, so
you know what to expect before you spend time on a change.

Everyone who takes part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Who can open issues and pull requests

Issues and pull requests are currently limited to collaborators of this repository. If you are not
a collaborator, you can still read the code and the docs, run Contractum locally, and report a
security problem privately (see below).

## Reporting a security problem

Do not open a public issue for a vulnerability. Report it privately through GitHub's private
vulnerability reporting: [open a security advisory](https://github.com/rkisliak/contractum/security/advisories/new).
[Security](docs/security.md) describes how Contractum is protected and checked.

## Licence

The Contractum server and the Python SDK and CLI have different licences: see [LICENSE](LICENSE)
and [clients/python/LICENSE](clients/python/LICENSE). Read them before you contribute.

## Set up a development environment

- [Local stack](docs/dev/local-stack.md): run, test and reset the whole platform with Docker
  Compose (`./run.sh`, or `.\run.ps1` on Windows).
- [Architecture guide](docs/dev/architecture/README.md): how the services fit together.
- [API design decisions](docs/dev/api-design-decisions.md): the conventions every route follows.
- [CI and GitHub](docs/dev/ci-and-github.md): the workflows, the security scanners and how to run
  them locally.
- [SDK and CLI](clients/python/README.md): the `contractum` Python client in `clients/python/`.

`bash scripts/ci.sh` runs the same lint, type, unit, integration and UI checks as CI on your
machine.

Run `sh scripts/setup-hooks.sh` once per clone. It turns on the git hooks of the docs gate (see
below and [The docs gate](docs/dev/ci-and-github.md#the-docs-gate)).

## What a change must pass

A pull request into `main` merges only when all of these are true:

- **Required checks are green:** `ci-ok` (lint, typing, import rules, unit and integration tests,
  UI checks and every image build), `docs-lint`, `docs-build`, `trivy-fs-gate`,
  `dependency-review`, `gitleaks`, `pip-audit` and `pnpm-audit`.
- **Code scanning is clean:** no new CodeQL alert of high severity or above.
- **Every review thread is resolved.**

The project also holds every change to these rules:

- **Security comes first.** Fix the root cause of a scanner finding. Do not hide it with an
  exclusion, an ignore entry or a suppression comment. A proven false positive goes on the matching
  ignore list only with a written reason, and an expiry date where the format allows one.
- **Coverage stays at 98% or more** for the whole project, measured from real unit and integration
  tests together (`scripts/coverage_gate.py`). New code aims for 100%. The SDK in
  `clients/python/` has its own 98% floors in the `sdk` workflow.
- **Docs stay current.** A change a reader could notice updates the matching page under `docs/` or
  `README.md` in the same pull request. The pull request template asks you to confirm this. The
  docs checks are the ones in [CI and GitHub](docs/dev/ci-and-github.md): markdownlint, typos,
  lychee, `zensical build --strict` and the docs gate. The gate fails a change to the API, the SDK
  and CLI, the UI, the settings, the Helm chart or the release workflow that changes no doc. A
  change with no reader-visible effect ends its pull request description with
  `Docs-Impact: none — <reason>`.
- **No secrets or personal data** in code, docs, tests or commit messages.

## Commit messages and pull requests

Commit messages, pull request titles and squash-merge subjects follow
[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):

```text
type(scope): description
```

- **type** says what kind of change it is. Common types: `feat` (a new feature), `fix` (a bug
  fix), `docs`, `refactor`, `test`, `build`, `ci`, `chore` and `perf`.
- **scope** is optional and names the part that changes, for example `sdk`, `ui`, `contracts` or
  `helm`.
- **description** is a short summary in the imperative mood, for example
  `fix(sdk): keep contract files byte-exact in git`.
- A breaking change adds `!` before the colon (`feat(api)!: remove the v0 routes`) or a
  `BREAKING CHANGE:` footer that explains what breaks.
- Dependency updates use `build(deps):`, for example `build(deps): update FastAPI to 0.165.0`.

How pull requests work:

- Work on a branch and open a pull request into `main`. Changes reach `main` only through pull
  requests.
- Fill in the pull request template: a summary of what changes and why, the docs status, and the
  checks you ran.
- Pull requests are merged with **squash merge** only, and `main` keeps a linear history. The pull
  request title becomes the commit subject on `main`, so it must follow the format above.
- The branch is deleted after the merge.
- Dependency updates come from Dependabot. Nothing merges automatically.
