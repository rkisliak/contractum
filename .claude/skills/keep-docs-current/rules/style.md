# Style details: word list, examples and where the guides disagree

Read this when you write more than a sentence or two, or when Vale flags a word. The short rules
are in section 4 of `SKILL.md`.

## Word list

### Internal jargon: replace in reader docs

These words mean something to the team or to agents, not to readers. Vale's `Contractum.Jargon`
rule warns on them. In `docs/dev/`, an architecture term may stay if the page defines it once
in plain words.

| Avoid | Write instead |
|-------|---------------|
| seam | boundary, extension point (or say what plugs in) |
| PDP, policy decision point | the access check (identity decides who may do what) |
| outbox | "each change writes an event in the same transaction" (dev docs: define once) |
| tripwire | safety check |
| lane | CI job, track |
| tracer, tracer bullet | end-to-end check, first working slice |
| quorum | agreement, majority |
| wave | batch, step |
| spike | experiment, prototype |
| plumbing | wiring, setup |
| load-bearing | required, essential |
| footgun | pitfall |
| fail-closed / fail-open | "refused when the check cannot run" / "allowed when the check cannot run" |
| first-class | built-in, fully supported |
| out of the box | by default |
| leverage, utilize | use |
| in order to | to |
| happy path | usual case |
| gray area | open question |
| dogfooding | using it ourselves (or drop) |
| plan, phase, wave, decision, requirement or task ids | the behaviour they describe |

### Filler and tone

| Avoid | Why | Instead |
|-------|-----|---------|
| please | instructions are imperatives | "Run ...", "Set ..." |
| simply, just, easily, obviously, of course | the step may not be easy for this reader | drop the word |
| there is / there are (to open a sentence) | weak opening, hides the subject | start with the subject: "Three roles can publish." |
| you can (when it adds nothing) | longer, vaguer | the verb: "Filter by folder." |
| it is important to note that | filler | drop it, state the fact |
| click here, here, this link | meaningless out of context | name the page or task |
| above, below (for page position) | breaks when content moves | "the previous step", "the following table", or a link |
| we, our, let's | the reader is "you" | "you", or the product as subject |
| will (for normal behaviour) | the product does it now | present tense: "The service returns 404." |

### Time-anchored words

Write docs that stay true until the behaviour changes. Vale's `Contractum.Timeless` warns and
`Contractum.StaleRelease` blocks.

| Avoid | Instead |
|-------|---------|
| currently, at present, at the moment, for now, as of this writing | drop the word; state the behaviour |
| soon, eventually, in the future, in a future release, planned, coming | delete: docs describe what exists. Plans belong in issues |
| not yet, does not yet | "does not": a limitation is a fact until it changes |
| no release has been published, until/before the first release | the true release state, with how to get it |
| new, now (meaning "changed recently") | drop, or name the version: "From 1.2.0, ..." in release notes only |

Allowed because they are product words, not time words: "Latest" (a contract lifecycle state),
"the latest published version", the **New** menu, "New registration" in Entra.

### Product and UI terms

- **Contractum** for the product. `contractum` (code font) for the Python package and CLI.
- Role names as the product shows them: Global admin, Org admin, Project editor, Team reader,
  Policy Manager. Do not replace "admin" with "administrator" inside a role name.
- Lifecycle states as the product shows them: Draft, Preview, Latest, Unpublished, Deprecated.
- ODCS, CEL, JSONPath, JSON Pointer, PostgreSQL, Kubernetes, Helm, Microsoft Entra ID, GitHub:
  this casing, in prose. Lowercase names are identifiers and go in code font.

## Before and after

### A stale release claim (the incident behind this skill)

Before (`docs/user/sdk-cli.md`, after v1.1.1 shipped):

> The `sdk-package` job of `.github/workflows/sdk.yml` builds and smoke-tests the wheel, the
> source archive and the image on every pull request that touches the client. Only the platform
> release workflow publishes them [...]. No release has been published yet.

After:

> Each release attaches the wheel, the source archive and `SHA256SUMS` to its GitHub Release
> and pushes the CLI image. Install from the release you want:
>
> ```bash
> gh release download vX.Y.Z --pattern '*.whl' --repo rkisliak/contractum
> ```

What changed: the false sentence is gone, the reader's task comes first, and the CI detail moved
to `docs/dev/ci-and-github.md`, where contributors look for it.

### One idea per sentence

Before (64 words):

> Each release also pushes a hardened, non-root image of the CLI (`clients/python/Dockerfile`,
> entrypoint `contractum`, working directory `/work`) to `ghcr.io/rkisliak/contractum-cli`,
> tagged with the platform version (`1.0.0`, or `1.1.0-rc.1` for a release candidate); a
> stable release also moves `latest`.

After:

> Each release pushes the CLI image to `ghcr.io/rkisliak/contractum-cli`. The tag is the
> platform version, for example `1.1.1`. A stable release also moves the `latest` tag. Pin a
> version in pipelines.

### Example before options

Before: a table of eight flags, then "for example, to test a file offline, ...".

After:

> Test a local file against a published contract:
>
> ```bash
> contractum test data/orders.parquet --contract payments.orders
> ```
>
> Add `--offline` to use only the cached contract. All options are in the
> [CLI reference](../../../../docs/user/reference/cli.md).

### Task first

Before: "The bootstrap admin secret is generated by the chart and can be used once, after which
the admin is required to choose a password."

After: "Sign in with the bootstrap admin secret, then choose a new password. The secret works
only once."

### Internal ids and jargon

Before: "Per D-86, pins go through the PDP seam (see Phase 7)."

After: "A pin records that you depend on a version. It never grants access: the service still
checks your own read access to the contract."

## Procedures

Rules (Google and Microsoft agree on these):

- Introduce the procedure with one sentence that says what it achieves, ending in a colon.
- Numbered steps, one action per step. Combine menu paths: **Administration > Users**.
- Put the condition or location before the action: "On **Administration > Users**, open the
  **New** menu."
- Start optional steps with "Optional:".
- Put the result after the action, in the same step: "Sign in with the bootstrap secret. The
  app asks you to choose a new password."
- Put a command in a code block on its own line after the step that needs it. Say what it does,
  not "run the following command".
- A single step is a sentence, not a one-item list.
- Never repeat a procedure that exists on another page; link to it.

## Accessibility and international readers

- Alt text on every image says what the image shows, not "screenshot".
- Do not rely on colour or position alone ("the red button", "on the right").
- Spell out an abbreviation on first use on each page, unless readers know it (API, URL, JSON).
- Avoid idioms and Latin abbreviations: "for example", not "e.g."; "that is", not "i.e.".
- Dates as ISO dates (`2026-11-20`) in tables and settings; times with a time zone.

## Where the guides disagree

The order of authority is: the user's rules, then this project's conventions, then Google, then
Microsoft, then community skills. The decisions that matter most:

| Topic | Decision | Why |
|-------|----------|-----|
| Contractions | Do not use them | the docs use "do not" about 440 times and contractions 6 times; Google and Microsoft prefer contractions, but consistency wins and full forms are easier for non-native readers |
| Oxford comma | Do not use it, unless the list is ambiguous | the docs omit it about 215 to 11; Google and Microsoft require it |
| Heading case | Sentence case | Google, Microsoft and the project agree; UI labels and names keep their capitals |
| Bullets vs prose | Bullets and short sections are welcome | the project owner prefers them; no "no bullets" rule |
| Notes and alerts | GitHub alerts (`> [!NOTE]`), sparingly | must render on GitHub and on the site |
| "please" | Never in instructions | Google and Microsoft agree |
| Line wrapping | Keep the paragraph's existing wrapping; new pages about 100 columns | the project wraps by hand; one community skill forbids hard wraps |
| Dates | ISO dates | the project uses them in tables and settings; Google prefers "July 31, 2016" |
| "Latest", "new" | Allowed as product words | Google's timeless list bans "latest" and "new", which are a lifecycle state and a UI label here |
| Spelling | American in new text | Google; most of the docs already use it |
