---
name: keep-docs-current
description: Keep the user, admin and developer docs (docs/, README.md, mkdocs.yml docs site) in step with every code, API, UI, setting, Helm, CI or behaviour change; which page to update, how to regenerate the reference pages, the writing rules and the checks to run before committing.
---

# keep-docs-current

Use this on **every change that a reader could notice**: a route, a problem slug, a permission
or role, a UI screen or label, a setting or environment variable, a Helm value, a CLI command or
option, an SDK function, a CI workflow, a deploy or upgrade step. Updating the docs is part of the
change, in the same commit or the next one. The pull request template asks for "Docs updated" or
"Docs not needed because ...", so be ready to say which.

## 1. Where things live

The docs are split by reader. Pages must read correctly on GitHub and on the Zensical site.

| Folder or file | Reader | Holds |
|----------------|--------|-------|
| `README.md` | everyone | pitch, screenshots, "Pick your path" links, try it locally, licence |
| `docs/index.md` | everyone | site home page |
| `docs/security.md` | everyone | security overview (keep it an overview: no details, no deep links) |
| `docs/user/` | users, data engineers, Policy Managers | getting started, SDK and CLI, subscriptions, API guide, writing rules, UI guide |
| `docs/user/reference/` | users | **generated**: `cli.md`, `sdk-api.md`, `http-api.md` (never edit by hand) |
| `docs/admin/` | operators | Kubernetes deploy, first run and roles, Entra setup, settings, upgrade and backup, troubleshooting, security standard |
| `docs/dev/` | contributors | local stack, CI and GitHub, architecture, API design decisions, publishing |
| `mkdocs.yml` | site | navigation by audience: every new page needs a `nav:` entry |
| private notes repository | maintainers | dated audit and SonarQube reports, never in this repository |
| `<module>/CLAUDE.md`, `.claude/skills/` | agents | module notes and skills: update them when the behaviour they describe changes |

## 2. Change to page map

| You changed | Update |
|-------------|--------|
| A route, request or response shape | regenerate the HTTP reference (step 3); `docs/user/api-guide.md` if it changes how callers work; the module's `CLAUDE.md` |
| A problem slug | the slug list in `docs/user/api-guide.md`; `docs/admin/troubleshooting.md` if an operator may meet it |
| A role or permission | the roles table in `docs/admin/first-run.md` **and** the one in `docs/user/api-guide.md` (keep both equal); `docs/user/ui-guide.md` if what a role sees changes |
| Subscriber cards, notices, webhooks, email | `docs/user/subscriptions.md` |
| Rules, rule sets, CEL functions | `docs/user/writing-rules.md` |
| A UI screen, tab, button or label | `docs/user/ui-guide.md`; replace a screenshot in `docs/images/ui/` only if it is now wrong |
| A setting, env variable or secret | `docs/admin/settings.md`; `docs/admin/deploy-kubernetes.md` for Helm values and Secrets |
| Helm chart or deploy steps | `docs/admin/deploy-kubernetes.md`, `docs/admin/upgrade-backup.md` |
| Entra sign-in or Graph | `docs/admin/entra-setup.md` |
| CLI command, option or SDK function | regenerate the CLI and SDK reference (step 3); `docs/user/sdk-cli.md` for usage; `clients/python/README.md` if the quickstart changes |
| A CI workflow or repository setting | `docs/dev/ci-and-github.md` |
| Compose stack, `run.sh` or `run.ps1` | `docs/dev/local-stack.md`, and the "Try it locally" part of `README.md` |
| Architecture: a new service, event or data flow | the matching page in `docs/dev/architecture/` |
| A new security tool or control | `docs/security.md` (one line, overview only) and `docs/dev/ci-and-github.md` |
| A new page | `mkdocs.yml` nav, a link from `docs/index.md` or the audience's entry page, and a `README.md` "Pick your path" bullet if readers need it |
| A moved or renamed page | every inbound link: `grep -rn "old/path.md" --include='*.md' --include='*.py' --include='*.yml' --include='*.yaml' .` (code comments, tests and workflows link to docs too) |

## 3. Generated references (never edit by hand)

From the repository root:

- **HTTP API:** after any route change, run `uv run python scripts/export_openapi.py`, then
  `uv run --group docs python scripts/gen_http_reference.py`, and the UI client with
  `pnpm --dir ui gen:api`.
- **CLI and SDK:** `cd clients/python && uv run python scripts/gen_reference.py`.

Each generator has a `--check` mode that CI runs; a stale page fails the build.

## 4. Writing rules

- Write for the page's reader: short plain sentences, active voice, one idea per bullet.
- Describe what the system does today. No history ("we used to"), no plans, no promises.
- No decision ids (`D-12`, `VAL-08`), quick-task ids or planning-folder paths in reader docs.
- No internal file paths in user and admin docs, unless the reader must open that file.
- No personal data: no names, email addresses, home paths, real tenant or client ids.
- Product name: **Contractum**. Use the UI's exact labels in bold (**Subscriptions**, **Access**).
- Claims must be true in the code. When unsure, read the code; do not guess.
- GitHub-compatible syntax only: GitHub alerts (`> [!NOTE]`), plain fenced blocks, Mermaid; no
  snippets, tabs or site-only macros.
- Links are relative (`../admin/first-run.md#grant-access`); headings give GitHub-style anchors.

## 5. Checks before committing

Run these from the repository root. They are the same checks as `.github/workflows/docs.yml`:

```bash
docker run --rm -v "$PWD:/workdir:ro" davidanson/markdownlint-cli2:v0.24.2@sha256:c768e94d8bb161c449bde28f60656929ca1f02c3010a405f65c9cac71563a889
uv run --group docs typos
docker run --rm -v "$PWD:/repo:ro" -w /repo lycheeverse/lychee:0.24.2@sha256:e2d19e57cf6ab037026f20b8e449a1f30d9d7f81eef4194763aab2eab20bd28d --offline --config lychee.toml './**/*.md'
uv run --group docs python scripts/gen_http_reference.py --check
(cd clients/python && uv run python scripts/gen_reference.py --check)
uv run --group docs zensical build --strict && rm -rf site
uv run pytest tests/repo -q
```

`tests/repo` pins some doc paths, so a moved page fails there until the test is updated.

## 6. Bigger rewrites

For a new page or a rewrite of a whole section, have a second pass check every factual claim
against the code (the GSD `gsd-doc-verifier` agent, or `/gsd-docs-update`), and fix what it
finds before committing.
