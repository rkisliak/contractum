---
name: add-check
description: Teach the contractum test engine a new ODCS check by mapping a contract field to a CheckSpec in engine/plan.py and a kernel in engine/kernels.py, or to a Skipped reason, never reporting an unevaluated rule as passed.
---

# add-check

Use when `contractum test` must judge one more ODCS field (a property or quality-rule setting) or
when a field that is skipped today becomes evaluable. Work from `clients/python`. The engine
(`src/contractum/engine/`) is pure: it imports no `click`, `rich` or network code, and never imports
server code.

1. **Decide the outcome first.** Every contract field ends in exactly one of: a `CheckSpec` that a
   kernel judges, or a `Skipped(check_id, category, rule, reason, ...)` with a reason a data
   engineer can read. **Never report a rule the engine did not evaluate as passed**, and never drop
   it silently: a field the engine cannot judge (a `sql` or `custom` quality rule, an unmappable
   `physicalType`, nesting deeper than `MAX_DEPTH`) is a `Skipped` entry, which the report shows and
   `--fail-on-skipped` can turn into exit 1.
2. **Add the spec** as a frozen `_Check` dataclass in `engine/checks.py` (parameters only, no data)
   and add it to the `CheckSpec` union. A spec carries `check_id`, `category`, `rule`, `severity`
   and `masked` from `_Check`.
3. **Plan it** in `engine/plan.py`: add a planner `(builder, prop) -> CheckSpec | Skipped | None`
   and register it in `_PROPERTY_PLANNERS` in the fixed rule order (reports are ordered by property,
   then rule). Validate the contract value here and return `b.skip(column, rule, reason)` for a
   malformed one (a bound that is not a number, a pattern RE2 cannot run, `UNSUPPORTED_PATTERN`).
   `PlanOptions.schema_only` runs only the rules in `_SCHEMA_ONLY_RULES`; leave value rules out of it.
4. **Write the kernel** in `engine/kernels.py`: it takes a pyarrow column plus the contract's
   parameters and returns a `KernelResult` (a boolean failure mask, true where a row breaks the
   rule and never null; the counts; a message), or a skipped result with a reason when the column
   type cannot be judged. Nulls and NaN are ignored by value checks (only `required` fails on
   them). Regular expressions run on RE2 through pyarrow only (`engine/regex.py`); never `re` on
   data. Bind the spec to the kernel in `_COLUMN_KERNELS` in `engine/arrow_backend.py`; a check that
   is not a single-column kernel gets its own handler in `ArrowBackend.__init__`.
5. **Nested properties and lists.** Check paths such as `shipping.city`, `tags[]` and
   `lines[].sku` come from `property_paths` and `NestedView` in `engine/nested.py`; a column is
   located through `_locate`, so a new check works on nested paths when it is registered like the
   others. Add a nested case to `tests/unit/engine/test_nested.py`.
6. **Types.** A new physical or logical type goes through `engine/types.py`
   (`logical_accepts`, `parse_physical_type`, `vendor_to_arrow`); DDL files use the same map.
7. **Masking.** A failure sample goes through `engine/masking.py` only. A check on a classified
   column sets `masked` via `b.masked(prop)`; never put a raw value into a message, a report or a
   log line.
8. **Tests** (marker `unit`, no network): a planner case in `tests/unit/engine/test_engine_plan.py`,
   a kernel case with passing, failing, null and wrong-type columns, a skipped case that asserts the
   reason text, and a check that the report status is `skipped`, not `passed`. If the JSON shape of
   the report changed, regenerate the committed schema with
   `uv run python -m contractum.report.json_out --write-schema` and commit it.
9. **Docs.** Update "Test data" and "Limits" in `docs/user/sdk-cli.md`; the docs-to-CLI test
   (`tests/unit/test_readme_quickstart.py`) keeps the commands and flags honest.
10. **Gates (D-48).** The coverage floor is 98 percent, measured on unit plus integration runs
    combined (never quote a unit-only figure as the SDK figure). Run, from `clients/python`:
    `uv run pytest -m unit -q`, `uv run mypy`, `uv run ruff check . && uv run ruff format --check .`,
    `uv run lint-imports --config pyproject.toml`. Python 3.11 is the floor: nothing newer
    than 3.11 (no `type` statements or PEP 695 generics).
