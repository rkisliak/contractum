---
name: keep-docs-current
description: Keep the user, admin and developer docs (docs/, the READMEs, mkdocs.yml) true and readable with every code, API, UI, setting, Helm, CI, release or behaviour change. Which page to update, which page type to write, the writing rules with limits, the stale-statement sweep, the release docs pass, the docs gate and the checks to run before committing.
---

# keep-docs-current

Documentation is part of every change. A change that a user, admin or developer could notice
updates the docs it affects **in the same change or plan**, never later.

Use this skill when you:

- change a route, problem slug, role, permission, UI screen or label, setting, environment
  variable, Helm value, CLI command, SDK function, CI workflow, or a deploy or upgrade step;
- cut a release (see [the release pass](rules/release-pass.md));
- write or review any page in `docs/`, `README.md`, `clients/python/README.md` or
  `deploy/helm/contractum/README.md`;
- plan a GSD phase or quick task that changes behaviour (it needs a docs task).

The rules below come first. They draw on the Google and Microsoft style guides and Diátaxis;
where those disagree with this project, the project wins (see
[rules/style.md](rules/style.md#where-the-guides-disagree)).

## 1. The non-negotiable rules

1. **Same change.** Docs ship in the commit or plan that changes the behaviour.
2. **Sweep for stale statements.** Before you finish, search the touched docs for sentences the
   change makes false (section 6). Reword or remove them.
3. **Edit only what the change touches.** Never rewrite a whole page unless someone asks.
   Fix a nearby stale sentence when you see one; leave the rest of the page alone.
4. **Write for humans.** Short sentences, plain words, the task first, an example before the
   theory, no internal jargon and no planning ids (section 4).
5. **Every release gets a docs pass** over the components it changed (section 7).
6. **The docs gate decides, honestly.** Never bypass it (`--no-verify`) and never weaken its
   phrase list or impact rules to pass. A change with no docs impact says so with a
   `Docs-Impact: none — <reason>` commit trailer (section 10).
7. **Claims must be true in the code.** When unsure, read the code. Do not guess.

## 2. Where things live

Pages must read correctly on GitHub and on the Zensical site.

| Folder or file | Reader | Holds |
|----------------|--------|-------|
| `README.md` | everyone | pitch, screenshots, "Pick your path" links, try it locally, licence |
| `docs/index.md` | everyone | site home page |
| `docs/security.md` | everyone | security overview (an overview: no details, no deep links) |
| `docs/user/` | users, data engineers, Policy Managers | getting started, SDK and CLI, subscriptions, API guide, writing rules, UI guide |
| `docs/user/reference/` | users | **generated**: `cli.md`, `sdk-api.md`, `http-api.md` (never edit by hand) |
| `docs/admin/` | operators | Kubernetes deploy, first run and roles, Entra setup, settings, upgrade and backup, troubleshooting, security standard |
| `docs/admin/security/` | auditors | the ASVS review (an audit record: cites files and commits on purpose) |
| `docs/dev/` | contributors | local stack, CI and GitHub, architecture, API design decisions |
| `clients/python/README.md` | SDK users | install and quickstart of the `contractum` package |
| `deploy/helm/contractum/README.md` | operators | chart values summary |
| `mkdocs.yml` | site | navigation by audience: every new page needs a `nav:` entry |
| `<module>/CLAUDE.md`, `.claude/skills/` | agents | module notes and skills: update them when the behaviour they describe changes |

## 3. Change to page map

| You changed | Update |
|-------------|--------|
| A route, request or response shape | regenerate the HTTP reference (section 8); `docs/user/api-guide.md` if callers work differently; the module's `CLAUDE.md` |
| A problem slug | the slug list in `docs/user/api-guide.md`; `docs/admin/troubleshooting.md` if an operator may meet it |
| A role or permission | the roles table in `docs/admin/first-run.md` **and** the one in `docs/user/api-guide.md` (keep both equal); `docs/user/ui-guide.md` if what a role sees changes |
| Subscriber cards, notices, webhooks, email | `docs/user/subscriptions.md` |
| Rules, rule sets, CEL functions | `docs/user/writing-rules.md` |
| A UI screen, tab, button or label | `docs/user/ui-guide.md`; replace a screenshot in `docs/images/ui/` only if it is now wrong |
| A setting, env variable or secret | `docs/admin/settings.md`; `docs/admin/deploy-kubernetes.md` for Helm values and Secrets |
| Helm chart or deploy steps | `docs/admin/deploy-kubernetes.md`, `docs/admin/upgrade-backup.md`, `deploy/helm/contractum/README.md` |
| Entra sign-in or Graph | `docs/admin/entra-setup.md` |
| CLI command, option or SDK function | regenerate the CLI and SDK reference (section 8); `docs/user/sdk-cli.md`; `clients/python/README.md` if the quickstart changes |
| The release workflow or what a release ships | `docs/dev/ci-and-github.md`; every page that says how to install (`docs/user/sdk-cli.md`, `clients/python/README.md`, `docs/admin/deploy-kubernetes.md`) |
| A CI workflow or repository setting | `docs/dev/ci-and-github.md` |
| Compose stack, `run.sh` or `run.ps1` | `docs/dev/local-stack.md`, and the "Try it locally" part of `README.md` |
| Architecture: a new service, event or data flow | the matching page in `docs/dev/architecture/` |
| A new security tool or control | `docs/security.md` (one line, overview only) and `docs/dev/ci-and-github.md` |
| A new page | `mkdocs.yml` nav, a link from `docs/index.md` or the audience's entry page, and a `README.md` "Pick your path" bullet if readers need it |
| A moved or renamed page | every inbound link: `grep -rn "old/path.md" --include='*.md' --include='*.py' --include='*.yml' --include='*.yaml' .` (code comments, tests and workflows link to docs too) |

## 4. Writing rules

Write for the page's reader. Each rule has a check you can apply.

### Sentences

- One idea per sentence. Aim for 15 to 20 words; split any sentence over 25. (Vale warns
  above 30.)
- Active voice, present tense, second person: "Run `contractum login`", "The service
  refuses the publish". Not "The publish will be refused by the service".
- Address the reader as "you". Do not write "we", "our" or "let's".
- Start instructions with a verb. Drop "please", "simply", "just", "easily", "obviously".
- No contractions: write "do not", "cannot" (the existing docs do).
- No Oxford comma: "folders, contracts and actions". Add the comma only when the list is
  ambiguous without it.
- American spelling in new text ("organization", "normalize"). Never respell a section you do
  not otherwise touch.

### Paragraphs and pages

- Open each page with one or two sentences: what it covers and who it is for.
- Put the task first, then the conditions, then the background. Front-load keywords.
- Show a copy-paste example **before** the list of options.
- Keep paragraphs to five sentences or fewer; three is better.
- One page, one job (one page type, section 5). Link instead of repeating.
- Describe what the system does today. No history ("we used to"), no plans, no promises.

### Structure

- Headings in sentence case: "Back up the databases", not "Back Up The Databases". UI labels,
  product and role names keep their capitals ("The **Notices** tab", "Org admin").
- One H1 per page. Do not skip heading levels. No period at the end of a heading.
- Numbered steps for procedures, one action per step (section 5 and
  [rules/page-types.md](rules/page-types.md)).
- Bullets for three or more parallel items. Short sections are good.
- Tables only to compare items or to list reference facts (settings, roles, slugs). Never use a
  table as layout for prose.

### Formatting

- UI labels in bold with the exact UI text: **Subscriptions**, **Access**, **New**.
- Code font for commands, file names, paths, identifiers, values, environment variables, problem
  slugs and service names (`postgres`, `cel-2`).
- Link text says where it goes: `see [Deploy on Kubernetes](../admin/deploy-kubernetes.md)`,
  never `click [here](...)`.
- Product name: **Contractum** (never the old name).
- Keep the line wrapping of the paragraph you edit. New pages wrap prose at about 100 columns.

### Content that never goes in reader docs

- Internal ids: decision, requirement, threat, plan, phase or quick-task ids (`D-12`, `VAL-08`,
  `T-15-24`, `04-02`, `Phase 7`, `261121-psn`) and `.planning/` paths.
- Internal jargon (seam, PDP, outbox, lane, tracer, quorum, wave, tripwire, spike). Use the plain
  word from [the word list](rules/style.md#word-list). Contributor pages may use an
  architecture term after defining it once.
- Internal file paths in user and admin pages, unless the reader must open that file.
- Personal data: no names, email addresses, home paths, real tenant or client ids.

[rules/style.md](rules/style.md) has the full word list and before/after examples.

## 5. Pick the page type

Every page, and every section of a long page, is one of four types (Diátaxis). Ask two questions:
does the reader want to **do** something or **understand** something? Are they **learning** or
**working**?

| Type | Reader wants | Contractum examples | Must not contain |
|------|--------------|---------------------|------------------|
| Tutorial | to learn by doing, with a guaranteed result | `docs/user/getting-started.md`, `docs/dev/local-stack.md` first run | choices, edge cases, long explanation |
| How-to guide | to finish a real task | `docs/admin/upgrade-backup.md`, `docs/admin/entra-setup.md`, `docs/user/subscriptions.md` | teaching, full option lists |
| Reference | to look up a fact | `docs/user/reference/*`, `docs/admin/settings.md`, the roles and slug tables | instructions, opinions |
| Explanation | to understand why | `docs/dev/architecture/*`, `docs/dev/api-design-decisions.md`, `docs/security.md` | step-by-step instructions |

Templates for each type are in [rules/page-types.md](rules/page-types.md). When a page mixes
types, keep the mix in separate sections and link between them.

## 6. Stale-statement sweep (every change)

After you edit the code and the docs, and before you commit:

1. List the docs the change touches (section 3), plus any page that mentions the changed thing:

   ```bash
   git grep -n -i -e "<changed name>" -- docs '*.md' ':!docs/user/reference'
   ```

2. Search those pages for time-anchored and release words:

   ```bash
   git grep -n -i -E "not yet|no release|first release|coming|soon|currently|planned|in a future|for now|at the moment|until|before|deprecated|removed|v[0-9]+\.[0-9]+" -- <pages>
   ```

3. For each hit, ask: is it still true after this change and after the latest release?
   - False: rewrite it as a plain statement of what is true now, or delete it.
   - True but time-anchored ("currently supports"): drop the time word ("supports").
   - Old version numbers in examples: use the current one, or a placeholder (`vX.Y.Z`) if the
     example must not age.
   - A removed option or command: remove every mention, including tables and examples.
4. Run the docs gate (section 10). Its Vale errors catch the known stale release phrases; it
   cannot catch every false sentence, so step 3 is still yours.

## 7. Release docs pass

Every release, before the tag: sweep the docs of every component the release changed, check the
release state, the version numbers and "upcoming" wording, and run the gate. The checklist is in
[rules/release-pass.md](rules/release-pass.md).

## 8. Generated references (never edit by hand)

From the repository root:

- **HTTP API:** after any route change, run `uv run python scripts/export_openapi.py`, then
  `uv run --group docs python scripts/gen_http_reference.py`, and the UI client with
  `pnpm --dir ui gen:api`.
- **CLI and SDK:** `cd clients/python && uv run python scripts/gen_reference.py`.

Each generator has a `--check` mode that CI runs; a stale page fails the build. To change the
text of a generated page, change its source (docstrings, OpenAPI descriptions) and regenerate.

## 9. GitHub-compatible syntax

- Alerts: GitHub alerts only (`> [!NOTE]`, `> [!WARNING]`). At most one or two per page; never
  stack them. Put the warning before the step it applies to.
- Plain fenced code blocks with a language (`bash`, `yaml`, `python`, `json`). One command per
  line; no shell prompt (`$`) so the block copies cleanly.
- Diagrams are PNG images: source in `docs/diagrams/<name>.mmd`, rendered with
  `scripts/render-diagrams.sh` into `docs/images/diagrams/<name>.png`. Never a live ` ```mermaid `
  block (it clipped labels in some browsers). Every image has alt text that states what the image
  shows.
- No snippets, tabs or site-only macros.
- Links are relative (`../admin/first-run.md#grant-access`); headings give GitHub-style anchors.
  Renaming a heading breaks its anchor: grep for `#old-anchor` and fix the links.

## 10. The docs gate

`scripts/docs_gate.py` runs in the pre-commit and commit-msg hooks, in the `docs-gate` job of
`.github/workflows/docs.yml` and in `tests/repo`. Turn the hooks on once per clone:

```bash
sh scripts/setup-hooks.sh
```

It sets `git config core.hooksPath .githooks` and runs `python3 scripts/docs_gate.py --sync`, which
downloads the Vale packages pinned in `.vale.ini` and checks their SHA-256 (use it, not
`vale sync`: Vale 3.28 ignores the pin). The pre-commit hook needs Vale 3.28.1 as `vale` on `PATH`,
or a running Docker (the gate then uses the pinned official image); without either it stops with
exit 2 and the setup steps. Commits that stage no doc do not need Vale. The developer page is
"The docs gate" in `docs/dev/ci-and-github.md`.

It checks three things:

- **Stale release claims.** When a `v*` tag exists, no doc may say there is no release. The
  phrase list is `.vale/styles/Contractum/StaleRelease.yml`.
- **Docs impact.** A commit that touches a documented surface (API routes, the SDK and CLI, UI
  features and routes, Helm values, settings, the release workflow) must also touch a doc, or
  carry the trailer. The list is `SURFACES` in `scripts/docs_gate.py`; the failure names the pages
  that usually need a word. Tests and version-pin-only diffs do not count.
- **Prose (Vale)** on the changed doc files: errors block (stale release claims, internal ids,
  the old product name); warnings are shown for you to fix in the lines you touched.

How to pass it honestly:

- Update the docs. That is the normal way.
- If the change really has no reader-visible effect, add the trailer as the last paragraph of
  the commit message and give a real reason:

  ```text
  Docs-Impact: none — internal refactor of the retention job, no behaviour change
  ```

  "none — n/a", "none — later" or "none — docs in a follow-up" are not reasons. Docs that are
  due go in this change.
- In a pull request, put the trailer at the end of the description: the pull request template
  shows where, the check re-runs when the description is edited, and the squash commit on `main`
  keeps it. The template's "Docs updated" or "Docs not needed because ..." box must agree.
- A Vale false positive on a correct sentence: add the term to
  `.vale/styles/config/vocabularies/Contractum/accept.txt`, or reword. Do not switch a rule off,
  and never use `<!-- vale off -->` to hide a real finding.
- Never bypass the hooks (`--no-verify`) and never edit the phrase list or the surface list to
  make your change pass. Changing the gate itself is its own reviewed change.

## 11. Use GSD for factual accuracy

- Every GSD plan that changes behaviour has a docs task naming the pages from section 3. The
  verifier fails a phase whose docs contradict the shipped behaviour.
- `/gsd-docs-update --verify-only` checks the existing docs against the code and writes nothing.
  Run it after a large change and before a release; fix what it reports by hand, or let
  `gsd-doc-writer` fix those claims in `fix` mode.
- Do not run `/gsd-docs-update` in generation mode or with `--force` here. Its canonical
  templates (`docs/ARCHITECTURE.md`, `docs/guides/`, `docs/api/`, `docs/testing/` and so on) do
  not match the `docs/user|admin|dev` layout. Decline every proposed new canonical doc, and
  never let `gsd-doc-writer` create or "supplement" a page with template sections. New pages
  follow section 5 and [rules/page-types.md](rules/page-types.md) and get a `mkdocs.yml` entry.
- `gsd-doc-writer` loads this skill: its rules override the writer's built-in templates.

## 12. Reader test (new or large pages)

For a new page, or a change that rewrites more than one section:

1. Write five to ten questions a real reader of that page would ask ("How do I rotate the
   signing key?", "Which role can publish?").
2. Give a fresh subagent **only the page** (no repository access, no conversation) and the
   questions. Ask it to answer each one, quote the sentence it used, and list anything unclear
   or any knowledge the page assumes.
3. Fix every wrong answer, unclear passage and unstated assumption. Repeat until the answers are
   right and no new gaps appear.
4. Then have a second, independent pass check every factual claim against the code (or run
   `/gsd-docs-update --verify-only`), and fix what it finds before committing.

## 13. Checks before committing

Run these from the repository root. They match `.github/workflows/docs.yml`:

```bash
python3 scripts/docs_gate.py --staged          # or --all for every doc (the release pass)
docker run --rm -v "$PWD:/workdir:ro" davidanson/markdownlint-cli2:v0.24.2@sha256:c768e94d8bb161c449bde28f60656929ca1f02c3010a405f65c9cac71563a889
uv run --group docs typos
docker run --rm -v "$PWD:/repo:ro" -w /repo lycheeverse/lychee:0.24.2@sha256:e2d19e57cf6ab037026f20b8e449a1f30d9d7f81eef4194763aab2eab20bd28d --offline --config lychee.toml --extensions md .
uv run --group docs python scripts/gen_http_reference.py --check
(cd clients/python && uv run python scripts/gen_reference.py --check)
uv run --group docs zensical build --strict && rm -rf site
uv run pytest tests/repo -q
```

`tests/repo` pins some doc paths, so a moved page fails there until the test is updated.

Before you call the change done, answer yes to each:

- [ ] Every page from the section 3 map is updated, in this change.
- [ ] The stale-statement sweep (section 6) found nothing false.
- [ ] Only the touched sections changed.
- [ ] Every new command and example was run, or copied from a test that runs it.
- [ ] The docs gate passes without `--no-verify` and without a weakened rule.
