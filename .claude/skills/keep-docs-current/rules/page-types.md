# Page types and templates

Every page, or every section of a long page, serves one reader need (Diátaxis). Pick the type
before you write, then use its template.

## Pick the type

| | The reader is learning | The reader is working |
|---|---|---|
| **The reader wants to do something** | Tutorial | How-to guide |
| **The reader wants to understand or look up something** | Explanation | Reference |

If a section drifts into another type (a how-to that starts explaining design, a reference
table that grows instructions), move that part to the right page and link to it.

## Where each type lives in this repo

| Folder | Mostly | Also |
|--------|--------|------|
| `docs/user/` | how-to guides (`subscriptions.md`, `writing-rules.md`, `api-guide.md`, `ui-guide.md`) | one tutorial (`getting-started.md`); the generated reference in `reference/` |
| `docs/admin/` | how-to guides (`deploy-kubernetes.md`, `entra-setup.md`, `upgrade-backup.md`, `troubleshooting.md`, `first-run.md`) | reference (`settings.md`, the roles table in `first-run.md`) |
| `docs/dev/` | explanation (`architecture/`, `api-design-decisions.md`) | how-to (`local-stack.md`, `ci-and-github.md`) |
| `README.md` | an overview and links ("Pick your path") | a short "Try it locally" tutorial |
| `docs/security.md` | explanation (overview only) | |

## Tutorial

For a reader who is new and wants a first success. You choose the path; the reader follows it
and it always works.

Rules:

- One path, no choices. Pick sensible defaults and say so in one line.
- Every step produces a visible result. Show it ("You see the **Home** page with your roles").
- No explanations longer than a sentence. Link to the explanation page instead.
- Test it from scratch before you merge it.

Template:

~~~markdown
# <Do the thing> for the first time

This page walks you through <outcome>. It takes about <N> minutes. You need <prerequisites>.

## Before you start

- <prerequisite, with a link to how to get it>

## <Step group 1, a verb phrase>

1. <action>.

   ```bash
   <command>
   ```

   You see <result>.

2. <action>. <result>.

## <Step group 2>

...

## Next steps

- <link to the how-to guide for the real task>
- <link to the explanation>
~~~

## How-to guide

For a reader who knows what they want and needs the steps. The title names the task.

Rules:

- Title is the task as a verb phrase: "Rotate the signing key", "Connect another tenant".
- Start with when to use it and what you need, in one or two sentences.
- Conditions before actions: "If you use your own ingress, ...".
- Show the copy-paste command first; put the option list after it, or link to the reference.
- End when the task is done. Say how to check it worked.
- Troubleshooting for this task goes at the end, or in `docs/admin/troubleshooting.md`.

Template:

~~~markdown
## <Do the task>

<When you need this, in one sentence.> You need <role or access> and <tool>.

1. <action>:

   ```bash
   <command>
   ```

2. <action>. <result>.
3. Optional: <action>.

To check it worked, <check>.

> [!WARNING]
> <Only if a step can lose data or lock someone out. Put it before that step instead when you can.>
~~~

## Reference

For a reader who needs a fact while working. Complete, consistent, dry.

Rules:

- Structure follows the thing it describes (one row per setting, one entry per slug).
- Every entry has the same fields in the same order: name, default, allowed values, effect.
- Facts only: no instructions beyond "see <how-to>", no opinions, no history.
- Generate it from the code when possible (`docs/user/reference/` is generated; never edit it).
- Tables are right here: they compare items across the same fields.

Template:

~~~markdown
## <Group of settings>

| Setting | Default | Allowed | What it does |
|---------|---------|---------|--------------|
| `DC_EXAMPLE` | `14` | 0 to 365 | <effect, in present tense> |

To change a setting on Kubernetes, see [Deploy on Kubernetes](deploy-kubernetes.md#settings).
~~~

## Explanation

For a reader who wants to understand why the system works the way it does.

Rules:

- Answer one question per section ("Why does an unreadable contract return 404?").
- Explain the reason and the trade-off, in plain words. Define each technical term once.
- No step-by-step instructions; link to the how-to guide.
- Diagrams help here. Use a PNG diagram with alt text (section 9 of `SKILL.md`).
- Describe the design as it is. Rejected options are fine to mention ("X is used instead of Y
  because ..."); plans are not.

Template:

~~~markdown
## <The question, as a reader would ask it>

<One-paragraph answer.>

<Why: the reason, and what it costs.>

![<What the diagram shows>](../images/diagrams/<name>.png)

For the steps, see [<how-to>](<link>).
~~~

## README files

A README is an entry page: what this is, the shortest way to try it, where to go next.

- First paragraph: what it is and who it is for.
- Shortest working install or quickstart, copy-paste ready, for the released version.
- Links to the full docs instead of repeating them.
- Nothing about unreleased plans.
