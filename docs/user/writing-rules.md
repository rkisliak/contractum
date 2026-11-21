# Writing rules

This page is for Policy Managers. It explains how to write rules that check contracts, how rule sets
are versioned and rolled out, and how the results show up for contract editors.

A rule is data, not code. It names the parts of a contract to look at (a JSONPath **target**) and a
true or false test for each part (a CEL **condition**). Nobody can upload a script, and a rule
cannot read files, call the network or loop forever.

To write rules you need the **Policy manager** role on a folder. It covers that folder and every
folder below it. Global admins can do the same everywhere.

## A rule's fields

| Field | What it does |
|-------|--------------|
| `key` | A stable name inside the rule set: lower case letters, digits and dashes, starting with a letter, at most 63 characters (for example `pii-needs-classification`). |
| `description` | Optional notes for other authors. |
| `target` | A JSONPath that selects the nodes to check. The default is `$`, the whole document. |
| `condition` | A CEL expression. It must return `true` for a node to pass. |
| `severity` | `error`, `warning` or `info`. Only `error` can block a publish. |
| `message` | The fixed text people see when the rule fails, 1 to 500 characters. It cannot include values from the document. |
| `match` | `each` (the default): every selected node must pass, and each failing node is its own finding. `any`: at least one node must pass. |
| `on_empty` | What happens when the target selects nothing: `pass` (the default) or `fail`. |
| `fixtures` | Example documents. At least one must `pass` and at least one must `fail`. They prove the rule does what you mean before it can be published. |

A rule set also has a `dsl_version`. New rule sets use `cel-2`. The older `cel-1` exists only so
that old published versions keep working exactly as before.

In the UI, the rule set's **Draft** tab has a form with these fields. The same content can be sent
as JSON or YAML to `PUT /api/v1/validation/rule-sets/{rule_set_id}/draft`.

## Where a rule looks: targets

Targets use standard JSONPath (RFC 9535). Common targets for ODCS contracts:

| Target | Selects |
|--------|---------|
| `$` | the whole contract |
| `$.schema[*]` | every table (ODCS schema object) |
| `$.schema[*].properties[*]` | every column (ODCS property) |

A target that selects nothing is not an error; `on_empty` decides. A target may select at most
10,000 nodes by default, or 100 when the condition reads the whole `contract` or `previous` document.
The JSONPath `match()` and `search()` functions are not allowed; use CEL `matches()` for regular
expressions.

Prefer a narrow target and a local condition. A condition that loops over the whole contract from
`$` can run out of its budget on a large contract.

## What a rule checks: conditions

Conditions are written in CEL (Common Expression Language), a small language for true or false
tests. It includes the standard CEL functions plus the string, list, math, optional and `cel.bind`
extensions. Regular expressions (`matches()`) use RE2, so they cannot hang.

### Variables a condition can use

| Variable | Value |
|----------|-------|
| `this` | the node the target selected |
| `contract` | the whole contract document |
| `ctx` | the folder the contract is in: `ctx.folder_kind` (`root`, `organization`, `project` or `team`), `ctx.folder_path` and `ctx.organization_id` |

`cel-2` rules can also use:

| Variable | Value |
|----------|-------|
| `ctx.operation` | `"save"` when a draft is saved or validated, `"publish"` at the publish gate |
| `previous` | the previous published version of the contract, or `null` on a first publish |
| `changes` | the list of classified changes since `previous`. Each change is a map with `code`, `class` (`breaking`, `warning` or `info`), `element_kind`, `path`, `pointer_before`, `pointer_after`, `attribute`, `before` and `after`. The list is empty on a first publish. |
| `refs` | reference facts that the platform looks up before the rules run: the maps `elements`, `servers`, `contracts`, `principals` and `teams`. Each value is `found`, `not_found` or `unverifiable`. Only `not_found` should fail a rule. |

### Helper functions

`cel-2` rules can call one platform helper:

- `dc.type_coherent(logicalType, physicalType)` returns `false` only when the two types clearly
  disagree, for example a logical `integer` with a physical `VARCHAR(10)`. It returns `true` for
  any type it does not know.

### Guard against missing fields

In CEL, reading a field that does not exist is an **error**, not `false`. For example,
`this.description != ''` on a column without a description gives the error `no_such_key`. An error
is its own outcome. It never counts as a pass, and in a rule that can block, an error blocks too.

So check first, then test:

- `has(this.description)` before you read `this.description`;
- `type(this.tags) == list` before you loop over `this.tags`;
- `previous == null || ...` before you read `previous`;
- `!(x in refs.elements) || refs.elements[x] != 'not_found'` before a map lookup.

Add fixtures for both shapes: with the field and without it.

## Examples

These examples come from the rules that ship with Contractum and from its tests.

### A column marked as a partition key needs its position

From the platform rule set `odcs-consistency`. Every check is guarded, so a column without
`partitioned` simply passes.

```yaml
dsl_version: cel-2
rules:
  - key: partition-key-position
    description: A partitioned property declares its partitionKeyPosition.
    target: "$.schema[*].properties[*]"
    condition: >-
      (type(this) == map && has(this.partitioned) && type(this.partitioned) == bool && this.partitioned)
      ? (has(this.partitionKeyPosition) && type(this.partitionKeyPosition) == int
      && this.partitionKeyPosition >= 1)
      : true
    severity: warning
    message: A partitioned property has no partitionKeyPosition.
    fixtures:
      pass:
        - document:
            schema:
              - name: events
                properties:
                  - {name: day, partitioned: true, partitionKeyPosition: 1}
        - document:
            schema:
              - name: events
                properties:
                  - {name: day}
      fail:
        - document:
            schema:
              - name: events
                properties:
                  - {name: day, partitioned: true}
```

### Logical and physical types fit together

Also from `odcs-consistency`. It uses the `dc.type_coherent` helper and only judges columns that
declare both types.

```yaml
dsl_version: cel-2
rules:
  - key: logical-physical-coherent
    description: The logicalType and the physicalType of a property do not clearly disagree.
    target: "$.schema[*].properties[*]"
    condition: >-
      (type(this) == map && has(this.logicalType) && type(this.logicalType) == string
      && has(this.physicalType) && type(this.physicalType) == string)
      ? dc.type_coherent(this.logicalType, this.physicalType)
      : true
    severity: warning
    message: The logicalType and the physicalType of a property do not fit together.
    fixtures:
      pass:
        - document:
            schema:
              - name: orders
                properties:
                  - {name: id, logicalType: integer, physicalType: BIGINT}
      fail:
        - document:
            schema:
              - name: orders
                properties:
                  - {name: id, logicalType: integer, physicalType: VARCHAR(10)}
```

### No breaking changes at all

Every contract already needs a new major version for a breaking change (see
[Findings and the report](#findings-and-the-report)). This stricter rule refuses breaking changes
even with a new major version. A fail fixture can carry a `previous` document; the platform works
out the `changes` between the two.

```yaml
dsl_version: cel-2
rules:
  - key: no-breaking
    target: "$"
    condition: "changes.all(c, c['class'] != 'breaking')"
    severity: error
    message: A published contract must not break its consumers.
    fixtures:
      pass:
        - document:
            schema:
              - name: orders
                properties: [{name: id}, {name: name}]
      fail:
        - document:
            schema:
              - name: orders
                properties: [{name: id}]
          previous:
            schema:
              - name: orders
                properties: [{name: id}, {name: name}]
```

## Rule sets and versions

A rule set is a named group of rules held by a folder (Root, an Organization, a Project or a Team).
It has one editable **draft** and numbered, immutable **versions**.

1. Create the rule set in a folder from the **Rule sets** tab of that folder. The name must be unique
   in the folder.
2. Edit the draft. Saving never fails because of a bad rule: it saves and shows a check report.
   **Check draft** runs the full check again.
3. **Publish** checks the draft and freezes exactly the checked content as the next version.

The check before publish runs, for each rule:

- **Size limits**: by default a condition of at most 2,048 bytes, a target of at most 512
  characters, and at most 200 rules per set.
- **Compile**: the condition compiles and returns true or false, and the target is valid JSONPath.
- **Fixtures**: each `pass` fixture passes and each `fail` fixture fails. At least one and at most 5
  of each by default, each at most 64 KiB.
- **Scale test**: the rule runs against a large made-up contract (100 tables of 100 columns). A
  rule that runs out of time, memory or budget there would fail on real contracts, so it is refused.

If any of these fails, the version is not published and the report shows which rule and why.

A published version never changes and a rule set with a published version cannot be deleted. To
retire a version, deprecate it. A deprecated version keeps working where it is already used, but it
cannot be newly attached to a contract.

## How rules apply down the folder tree

There are two ways a rule set applies to a contract:

- **Mandatory.** Turn on the switch on the rule set's **Rollout** tab. A mandatory set applies to
  every contract in its folder and in every folder below it. Nothing below can remove, pause or
  weaken it, and blocking inherited access on a folder does not stop it.
- **Attached.** A contract editor can attach extra published rule sets to one contract from the
  contract's **Rule sets** tab. A contract can use only rule sets from its own folder, a folder above
  it, or the platform. Attaching can add rules, never remove a mandatory one.

Contractum also ships platform rule sets that apply to every contract: `odcs-structural`,
`odcs-consistency`, `odcs-references` and `odcs-publication`. Every validation also checks the
official ODCS JSON Schema first. A schema violation always blocks.

### Warn first, then block

A mandatory version does not block at once. It warns first:

1. When you make a set mandatory, or publish a new version of a mandatory set, that version gets a
   start date: now plus the grace period. A Global admin sets the grace period under
   **Settings > Governance** (14 days by default; 0 blocks at once).
2. Until that date, the version runs and reports its findings as warnings. It never blocks.
3. From that date, its `error` findings block publishes.

While a newer version is still in its grace period, the older enforced version keeps blocking, and
the newer one is reported next to it. The **Rollout** tab shows each version's start date. **Enforce
now** makes a warning version block at once.

## Dry run and impact

Before you make a set mandatory or publish a new version, check what it would do to the contracts
that already exist:

- The rule set's **Impact** tab, or **Dry runs** in the menu, evaluates the Latest version of every
  published contract in scope as if a rule-set version (or the draft) were enforced now. It counts
  the contracts that would fail, would error or pass, and lists the failing ones with rule keys and
  locations.
- **Drift** lists published contracts whose Latest no longer passes the mandatory versions that apply
  now (drifted), or will not once a warning version starts to block (at risk).

Nothing is stored and nobody is notified. You see only the contracts your access allows.

## Findings and the report

Every validation returns a report. Editors see it in the draft editor; the report a version was
published with is kept for good and shown with the version.

Each rule ends as **pass**, **fail** or **error**. Fails and errors become findings. A finding shows:

- the rule set and version, the rule key, the severity and your message;
- the location in the document, as a JSON Pointer such as `/schema/0/properties/1`;
- whether it blocks, and for an error, its error code.

A finding **blocks** only when its rule has severity `error` and its rule set is enforced: a
mandatory version past its start date, or a set attached to the contract. A Global admin can pause a
platform rule set; a paused set still reports its findings but never blocks.
Warnings, info findings and anything from a version in its grace period never block. A contract
can be published only when it matches the ODCS schema and has no blocking finding.

The platform adds its own findings too. The most common one is `semver-breaking-change`: a breaking
change, such as a removed or renamed column, needs a new major version. The finding names the
smallest version that passes, for example `2.0.0`.

### Error codes

When a rule cannot be evaluated, its finding carries one of these codes:

| Code | Meaning | What to do |
|------|---------|------------|
| `no_such_key` | The condition read a field that does not exist. | Guard with `has(...)`. |
| `type_error` | An operator got a value of the wrong type. | Guard with `type(x) == ...`. |
| `invalid_argument` | A function got an invalid argument. | Check the inputs. |
| `not_boolean` | The condition did not return true or false. | Make it return a boolean. |
| `budget_exceeded` | The condition looped too much. | Narrow the target so each node does less work. |
| `memory_exceeded` | The rule used too much memory. | Simplify the rule. |
| `timeout` | The rule ran too long. | Simplify it; avoid scanning `contract` from every node. |
| `run_budget_exceeded` | The whole validation ran out of time before this rule. | Retry. If it repeats, tell your administrator. |
| `too_many_matches` | The target selected more nodes than allowed. | Narrow the target. |
| `target_error` | The target could not run on this document, for example because it is nested too deeply. | Check the document's shape. |
| `engine_error` | An unexpected engine error. | Tell your administrator. |
| `unresolvable_rule_set` | An attached rule set cannot be found or is not visible from the contract's folder. | Fix or detach the attachment. |

## More detail

The [validation and rules architecture page](../dev/architecture/04-validation-and-rules.md) covers
the sandbox, the limits and the internals. The [HTTP API reference](reference/http-api.md) lists the
rule-set routes and their permissions.
