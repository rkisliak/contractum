"""Report builders and a small JSON Schema checker for the report tests (tests only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from contractum.report.model import (
    CheckResult,
    ContractInfo,
    DatasetInfo,
    Report,
    SampleOut,
    ToolInfo,
    summarize,
)


def check(check_id: str, status: str, **fields: Any) -> CheckResult:
    """One check result; category and rule default to values that read naturally."""
    values: dict[str, Any] = {"category": "data", "rule": "required"}
    values.update(fields)
    return CheckResult(id=check_id, status=status, **values)  # type: ignore[arg-type]


def make_report(*checks: CheckResult, status: str | None = "latest", **dataset: Any) -> Report:
    """A report around the given checks with a summary computed from them."""
    return Report(
        tool=ToolInfo(version="0.1.0"),
        contract=ContractInfo(
            source="server",
            odcs_id="payments.orders",
            semver="1.1.0",
            status=status,
            content_sha256="a" * 64,
        ),
        dataset=DatasetInfo(
            **{"kind": "parquet", "location": "orders.parquet", "rows_total": 4, "rows_tested": 4}
            | dataset
        ),
        object="orders",
        summary=summarize(checks),
        checks=checks,
        started_at=datetime(2026, 11, 21, 9, 30, tzinfo=UTC),
        duration_ms=1250,
    )


def sample(row: int, value: str) -> SampleOut:
    return SampleOut(row=row, value=value)


def all_statuses() -> Report:
    """One check of each status; the failed one carries samples."""
    return make_report(
        check("orders.id.present", "passed", rule="present"),
        check(
            "orders.id.required",
            "failed",
            column="id",
            message="1 of 4 values are null",
            failed_rows=1,
            total_rows=4,
            failed_percent=25.0,
            samples=(sample(1, "None"),),
        ),
        check("orders.amount.range", "warning", column="amount", message="close to the limit"),
        check("orders.q[0]", "skipped", rule="quality", skipped_reason="sql rules are not run"),
        check("orders.type", "error", rule="type", message="could not be evaluated"),
    )


def schema_errors(
    instance: Any, schema: dict[str, Any], root: dict[str, Any] | None = None
) -> list[str]:
    """Violations of the JSON Schema subset that pydantic emits (no external validator needed)."""
    top = root or schema
    if "$ref" in schema:
        target: Any = top
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return schema_errors(instance, target, top)
    if "anyOf" in schema:
        options = [schema_errors(instance, option, top) for option in schema["anyOf"]]
        return [] if any(not found for found in options) else ["matches no anyOf branch"]
    errors: list[str] = []
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{instance!r} is not {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{instance!r} is not one of {schema['enum']}")
    errors.extend(_type_errors(instance, schema, top))
    return errors


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
    "array": (list,),
    "object": (dict,),
}


def _type_errors(instance: Any, schema: dict[str, Any], top: dict[str, Any]) -> list[str]:
    kind = schema.get("type")
    if kind is None:
        return []
    if not isinstance(instance, _TYPES[kind]) or (kind == "integer" and isinstance(instance, bool)):
        return [f"{instance!r} is not {kind}"]
    if kind == "array":
        return [
            problem
            for item in instance
            for problem in schema_errors(item, schema.get("items", {}), top)
        ]
    if kind != "object":
        return []
    errors = [f"missing {name}" for name in schema.get("required", []) if name not in instance]
    properties = schema.get("properties", {})
    for name, value in instance.items():
        if name in properties:
            errors.extend(f"{name}: {e}" for e in schema_errors(value, properties[name], top))
        elif schema.get("additionalProperties") is False:
            errors.append(f"unexpected {name}")
    return errors
