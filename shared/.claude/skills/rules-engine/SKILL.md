---
name: rules-engine
description: Author, evaluate or change declarative rules with the dc-rules library (rule fields, CEL bindings, the cel-1 and cel-2 surfaces, `dc.*` functions, JSONPath targets, error codes, engine isolation boundary) without weakening the fail-closed semantics.
---

# rules-engine

Use before changing anything under `shared/dc-rules/` or writing a rule (seeded or authored).

1. **A rule** (`dc_rules.model.RuleDef`, D-110; `extra=forbid`): `key` (`^[a-z][a-z0-9-]{0,62}$`,
   ASCII only), `description` (optional), `target` (JSONPath, default `$`), `condition` (CEL,
   must produce bool), `severity` (`error`, `warning`, `info`), `message` (1 to 500 Unicode code
   points, static text, no templating), `match` (`each` default: one finding per failing node;
   `any`: at least one node must pass), `on_empty` (`pass` default, `fail`), `fixtures`
   (`pass` and `fail` lists; `pass` is the alias of `pass_`). There is no field that decides what an
   evaluation error means: an error is its own outcome, never a pass or a fail (D-139, RULE-09).
   Limits that are deployment settings live in `Limits` (`max_expression_bytes` 2,048 UTF-8 bytes,
   `max_target_chars` 512 code points); the model only keeps sanity caps.
   `canonical_json` and `content_sha256` give the stable content hash of a rule set.
2. **Bindings** (D-111, D-152): `this` (the matched node), `contract` (the whole normalised document)
   and `ctx` (`folder_kind`, `folder_path`, `organization_id`), all dynamic. A **cel-2** rule also
   sees `ctx.operation` (`"save"` or `"publish"`, quick 261119-rpi): runs bind their mode, batch
   binds `publish`, a cel-2 fixture binds its own `ctx.operation` or `save`, the scale probe binds
   `publish`, and cel-1 never sees the key (the worker strips `CEL2_CTX_KEYS`). Guard it with
   `!has(ctx.operation) || ctx.operation != 'publish' || (...)`. A cel-2 rule also
   sees `previous` (the raw baseline document in the same shape as `contract`, or `null` on a first
   publish), `changes` (the classified change list from `dc_odcs.diff`, JSON-native maps with the
   keys `code`, `class`, `element_kind`, `path`, `pointer_before`, `pointer_after`, `attribute`,
   `before`, `after`; always a list, never null) and `refs` (the maps `elements`, `servers`,
   `contracts`, `principals`, `teams`; keys are the strings as written in the document, values the
   tri-state `found` / `not_found` / `unverifiable`, or a map for `contracts`; `EMPTY_REFS` when the
   caller has no facts). The bindings are per job, computed once by the caller. Call
   `normalize_document` once per document first (the worker also does it over `previous`, `changes`
   and `refs`): an integer outside int64/uint64 otherwise makes every evaluation over that subtree
   an `invalid_argument` error.
   - **cel-1 vs cel-2.** `dsl_version` is `cel-1` or `cel-2`; `DSL_VERSION` (new drafts) is
     `cel-2`. cel-1 is a legacy environment kept so published, immutable versions evaluate exactly as
     before: it rejects `previous`, `changes`, `refs` and every `dc.*` function at compile time.
     Every cel-1 conformance case also holds under cel-2 (test-pinned). Each unit is compiled in the
     environment named by its `dsl_version` (`engine.compile(expr, dsl_version)`).
   - **Guard idioms.** `previous == null || previous.version == '1.0.0'` is safe on a first publish;
     `has(previous.x)` with a null `previous` is an evaluation error (never a pass).
     `changes.all(c, c['class'] != 'breaking')` forbids breaking changes. CEL maps have no `.get()`
     and a missing key is `no_such_key`, so guard a lookup:
     `!(x in refs.elements) || refs.elements[x] != 'not_found'`.
   - **Match cap.** A condition naming `contract` or `previous` (document-sized) uses
     `max_matches_with_contract`; `changes` and `refs` do not (`references_document_binding`).
3. **The cel-1 / cel-2 surface** (D-136): CEL standard library plus `ExtStrings`, `ExtOptional`,
   `ExtLists`, `ExtBindings`, `ExtMath`. No proto, no encoders, no `now()`, no random or I/O.
   `matches()` is RE2 (linear time). Parser limits: recursion depth 32 and 100,000 code points.
   The exact `cel-expr-python==0.1.4` pin is part of this contract.
   **`dc.*` functions** (cel-2 only, `dc_rules.functions`, D-154): platform-authored, pure
   `(string, string) -> bool` helpers. `dc.type_coherent(logicalType, physicalType)` is false only
   when a small conservative table says the pair clearly disagrees (`integer` with `varchar`); an
   unknown physical or logical type, an empty value and an input over 256 characters return true. To
   add one: write a pure, input-bounded implementation (no I/O, no state outside its arguments),
   append a `DcFunction` to `FUNCTIONS`, add a case in `test_cel2_surface.py` and
   `test_engine_conformance.py`. An exception inside one is the fixed `engine_error`; its text never
   reaches a result.
4. **Write rules that stay inside the budget.** The engine has a fixed iteration budget of about
   10,000 loop iterations per evaluation (a 9,999-element `all()` passes, 10,000 returns
   `budget_exceeded`) and it is not configurable. A rule such as
   `contract.schema.all(s, s.properties.all(p, ...))` passes small fixtures and fails on a large
   contract. Select tables or properties with the JSONPath `target` so each node gets its own
   budget, and keep the condition local (`this.properties.all(...)`).
5. **Guard missing data.** A missing key is an evaluation error (`no_such_key`), never `false`.
   Guard with `has(this.a.y)`, optional access (`this.?a.?y.orValue(3)`) or `cel.bind`; guard
   list indexes with `size()` (an out-of-range index maps to `engine_error`). Do not rely on
   `||` ordering, and parenthesise `!has(x) || A ? B : C`. Add fixtures for both the field-present
   and the field-absent shape.
6. **Targets** are strict RFC 9535 (python-jsonpath). The `match()` and `search()` filter
   functions are removed (they run on Python `re` and backtrack catastrophically); a path that
   uses either fails to compile with `target_invalid`. Use CEL `matches()` for regular
   expressions. `length()`, `count()` and `value()` still work. Zero matches is not an error
   (`on_empty` decides). Selection is lazy and capped: 10,000 matches, or 100 for a rule whose
   condition references `contract` (each evaluation converts the whole document); exceeding the
   cap is `too_many_matches`. A document too deep for the matcher is `target_error`.
7. **Error codes.** Compile time (author feedback only, raised as `RuleCompileError`):
   `compile_error` (syntax, undeclared name, wrong types, recursion or size limit, `previous`,
   `now()`), `not_boolean` (a concrete non-bool return type), `target_invalid`. Evaluation time
   (carried in findings, `ErrorCode`): `no_such_key`, `type_error`, `invalid_argument`,
   `budget_exceeded`, `memory_exceeded`, `timeout`, `not_boolean`, `too_many_matches`,
   `target_error`, `run_budget_exceeded`, `engine_error`, `unresolvable_rule_set`. Each code has a
   fixed English message in `ERROR_MESSAGES`. Findings and reports carry the code only: engine text
   can echo document values (for example an out-of-range integer) and is shown only as author
   feedback on fixtures.
8. **Import boundary.** `dc_rules` imports no `dc_*` package (import-linter "dc_rules is pure") and
   the package root never loads the engine. Only the worker (plan 05-03) imports `cel_engine`,
   `targets` and `evaluator`; the API process uses the model and ports only. Evaluation cannot be
   cancelled and its memory is unbounded, so it runs in worker subprocesses with a memory limit and
   a parent-enforced deadline (D-138).
9. **Changing the engine or JSONPath pin.** Both are exact `==` pins. Run the conformance suite
   (`test_engine_conformance.py`, parameterised over the `engine` fixture in `conftest.py`) and
   `test_targets.py` against the new pin or a second adapter; a red test means the behaviour the
   isolation model and error semantics rely on changed, so treat it as a design decision, not a
   test edit.
10. **The rule-set check** (`dc_rules.checks`, plan 05-04): `static_problems` (sizes, fixture counts,
    duplicates), `check_units` (compile, one unit per fixture, scale probe of 100 tables x 100
    columns; every unit is stamped with the set's `dsl_version`) and `assemble_check` build the per-rule report; the worker's check op answers the units
    and `Sandbox.check` runs them with `check_budget_s`. A pass fixture must evaluate to pass, a
    fail fixture to fail, and any rule error means "not as declared". The probe refuses a rule only
    on scale errors (`budget_exceeded`, `timeout`, `memory_exceeded`, `too_many_matches`, or an
    unfinished probe), never on `no_such_key`. A rule that is cubic in tables x columns passes small
    fixtures and fails the probe; `contract.schema.all(s, s.properties.all(...))` alone is 10,100
    iterations (100 x 100) and is refused by the probe; at the original 50 tables it was 5,050 and
    was not (plan 05-06, orchestrator decision).
11. **cel-2 fixtures and the probe.** A `Fixture` may carry `previous` and `refs` (rejected in a
    cel-1 set; absent keys are omitted from canonical JSON so old content hashes do not move; the
    optional `ctx.operation` is likewise omitted when unset and refused in a cel-1 set). A
    fixture's `changes` are derived from `(previous, document)` by the **caller-supplied
    `changes_for`** callable passed to `check_units(content, limits, changes_for=...)`: dc_rules
    imports no other platform package, so validation injects the `dc_odcs.diff` classifier. A fixture
    with `previous` and no `changes_for` (or one that raises) is the check problem
    `fixture_changes_unavailable`, never an empty `changes` list. The scale probe of a cel-2 rule binds
    `previous` (the probe document), 200 synthetic `changes` and 100-entry `refs` maps, so a valid
    cel-2 rule does not fail the publish-time check.
12. **Verify.**
    - `uv run pytest -m unit -q shared/dc-rules tests/repo/test_module_layout.py`
    - `uv run ruff check shared/dc-rules && uv run mypy shared/dc-rules/src`
    - `uv run lint-imports && uv lock --check`
