"""Turn a contract schema object into an ordered plan of checks.

``plan()`` is pure: it reads the ``SchemaObject`` accessors and returns frozen ``CheckSpec``
values plus ``Skipped`` entries for everything the engine cannot evaluate. Report order is the
contract's property order, a fixed rule order per property, the object-level checks and then the
object's quality rules; the lifecycle check is appended by ``build_report``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from contractum.engine.checks import (
    ArrayBounds,
    Category,
    CheckSpec,
    ColumnPresent,
    ExtraColumns,
    ExtraPolicy,
    FormatIs,
    InSet,
    Length,
    MultipleOf,
    NotNull,
    Pattern,
    PhysicalTypeIs,
    Plan,
    PrimaryKey,
    Range,
    Severity,
    Skipped,
    TypeIs,
    Unique,
)
from contractum.engine.masking import MaskMode, should_mask
from contractum.engine.nested import MAX_DEPTH, PropertyPath, property_paths
from contractum.engine.quality import build_quality_check, severity_of
from contractum.engine.regex import UNSUPPORTED_PATTERN, re2_supported
from contractum.engine.types import (
    FLOAT32_MAX,
    INTEGER_FORMAT_RANGES,
    jdk_to_strptime,
    physical_type_known,
    real_number,
    whole_number,
)
from contractum.odcs import Property, QualityRule, SchemaObject

_NO_SITE = PropertyPath(Property(name=""), "", (), 0)

LOGICAL_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "date", "timestamp", "time"}
    | {"array", "object", "map", "vector"}
)

_BOUND_KEYS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
_SCHEMA_ONLY_RULES = frozenset({"present", "type", "physical_type"})
_STRING_FORMATS = frozenset({"uuid", "email", "ipv4", "ipv6"})
_DATE_LOGICAL_TYPES = frozenset({"date", "timestamp", "time"})


@dataclass(frozen=True)
class PlanOptions:
    """Choices that shape a plan (never data)."""

    extra: ExtraPolicy = "warn"
    strict_types: bool = False
    mask: MaskMode = "auto"
    sampled: bool = False
    schema_only: bool = False
    max_depth: int = MAX_DEPTH


class _Builder:
    """Collects checks and skipped entries and keeps check ids unique."""

    def __init__(self, obj: SchemaObject, options: PlanOptions) -> None:
        self.obj = obj
        self.options = options
        self.checks: list[CheckSpec] = []
        self.skipped: list[Skipped] = []
        self.order: list[str] = []
        self._seen: set[str] = set()
        self.site: PropertyPath = _NO_SITE
        self.source_names: dict[str, str] = {}
        for prop in obj.properties:
            self.source_names[prop.name] = prop.source_name
            self.source_names.setdefault(prop.source_name, prop.source_name)

    def unique_id(self, base: str) -> str:
        candidate, number = base, 1
        while candidate in self._seen:
            number += 1
            candidate = f"{base}~{number}"
        self._seen.add(candidate)
        return candidate

    def rule_id(self, column: str | None, rule: str) -> str:
        prefix = self.obj.name if column is None else f"{self.obj.name}.{column}"
        return self.unique_id(f"{prefix}.{rule}")

    def masked(self, prop: Property) -> bool:
        return should_mask(prop.classification, self.options.mask)

    def site_masked(self) -> bool:
        """Whether samples of the property being planned are masked (a sensitive parent counts)."""
        return should_mask(self.site.classification, self.options.mask)

    def keep(self, entry: CheckSpec | Skipped | None) -> None:
        if entry is None:
            return
        if isinstance(entry, Skipped):
            self.skipped.append(entry)
        else:
            self.checks.append(entry)
        self.order.append(entry.check_id)

    def skip(
        self,
        column: str | None,
        rule: str,
        reason: str,
        category: Category = "constraint",
        severity: Severity = "error",
    ) -> Skipped:
        return Skipped(
            check_id=self.rule_id(column, rule),
            category=category,
            rule=rule,
            reason=reason,
            column=column,
            severity=severity,
        )


Planner = Callable[[_Builder, Property], CheckSpec | Skipped | None]

_NUMBER_FORMAT_RANGES: dict[str, tuple[float, float]] = {"f32": (-FLOAT32_MAX, FLOAT32_MAX)}
"""Number format widths with a value range; ``f64`` needs none."""


def _base(b: _Builder, prop: Property, rule: str, category: Category) -> dict[str, Any]:
    """The fields every per-property check carries (nested ones add their path steps)."""
    site = b.site
    nested = site.depth > 1
    return {
        "check_id": b.rule_id(site.column, rule),
        "category": category,
        "rule": rule,
        "column": site.column,
        "physical_name": None if nested else prop.physical_name,
        "masked": b.site_masked(),
        "steps": site.steps if nested else (),
    }


def _plan_present(b: _Builder, prop: Property) -> CheckSpec | None:
    if b.site.is_items:
        return None
    return ColumnPresent(**_base(b, prop, "present", "schema"))


def _plan_type(b: _Builder, prop: Property) -> CheckSpec | Skipped | None:
    declared = prop.logical_type
    if declared is None:
        return None
    logical = declared.lower()
    if logical not in LOGICAL_TYPES:
        return b.skip(
            b.site.column, "type", f"logicalType {declared!r} is not recognised", "schema"
        )
    return TypeIs(
        **_base(b, prop, "type", "schema"), logical_type=logical, strict=b.options.strict_types
    )


def _plan_physical_type(b: _Builder, prop: Property) -> CheckSpec | Skipped | None:
    declared = prop.physical_type
    if declared is None:
        return None
    if not physical_type_known(declared):
        reason = f"physicalType {declared!r} has no Arrow mapping"
        return b.skip(b.site.column, "physical_type", reason, "schema")
    return PhysicalTypeIs(**_base(b, prop, "physical_type", "schema"), physical_type=declared)


def _plan_required(b: _Builder, prop: Property) -> CheckSpec | None:
    if not prop.required:
        return None
    return NotNull(**_base(b, prop, "required", "schema"))


def _plan_unique(b: _Builder, prop: Property) -> CheckSpec | Skipped | None:
    if not prop.unique:
        return None
    if b.site.in_list:
        return b.skip(
            b.site.column, "unique", "unique is not evaluated inside array items", "schema"
        )
    return Unique(**_base(b, prop, "unique", "schema"))


def _plan_enum(b: _Builder, prop: Property) -> CheckSpec | None:
    if prop.enum_values is None:
        return None
    return InSet(**_base(b, prop, "enum", "constraint"), values=prop.enum_values)


def _plan_pattern(b: _Builder, prop: Property) -> CheckSpec | Skipped | None:
    if "pattern" not in prop.options:
        return None
    regex = prop.options["pattern"]
    if not isinstance(regex, str):
        return b.skip(b.site.column, "pattern", "pattern is not a text value")
    if not re2_supported(regex):
        return b.skip(b.site.column, "pattern", UNSUPPORTED_PATTERN)
    return Pattern(**_base(b, prop, "pattern", "constraint"), pattern=regex)


def _whole_bounds(*raws: Any) -> list[int | None] | None:
    """Each stated bound as a whole number (absent stays None); None if one is not whole."""
    bounds = [None if raw is None else whole_number(raw) for raw in raws]
    if any(raw is not None and bound is None for raw, bound in zip(raws, bounds, strict=True)):
        return None
    return bounds


def _plan_length(b: _Builder, prop: Property) -> CheckSpec | Skipped | None:
    lower, upper = prop.options.get("minLength"), prop.options.get("maxLength")
    if lower is None and upper is None:
        return None
    bounds = _whole_bounds(lower, upper)
    if bounds is None:
        return b.skip(b.site.column, "length", "minLength and maxLength must be whole numbers")
    return Length(
        **_base(b, prop, "length", "constraint"), min_length=bounds[0], max_length=bounds[1]
    )


def _plan_range(b: _Builder, prop: Property) -> CheckSpec | None:
    given = {key: prop.options.get(key) for key in _BOUND_KEYS}
    if all(value is None for value in given.values()):
        return None
    return Range(
        **_base(b, prop, "range", "constraint"),
        minimum=given["minimum"],
        maximum=given["maximum"],
        exclusive_minimum=given["exclusiveMinimum"],
        exclusive_maximum=given["exclusiveMaximum"],
    )


def _plan_multiple_of(b: _Builder, prop: Property) -> CheckSpec | Skipped | None:
    factor = prop.options.get("multipleOf")
    if factor is None:
        return None
    number = real_number(factor)
    if number is None or number <= 0:
        return b.skip(b.site.column, "multiple_of", "multipleOf must be a number greater than 0")
    return MultipleOf(**_base(b, prop, "multiple_of", "constraint"), factor=number)


def _plan_numeric_format(
    b: _Builder, prop: Property, fmt: str, logical: str
) -> CheckSpec | Skipped | None:
    """Integer and number formats are widths, checked as a value range (D-39).

    A value outside the width is a warning by default and an error with strict types. ``f64``
    plans nothing (every float64 value fits); a format without a known width is skipped.
    """
    if logical == "number" and fmt == "f64":
        return None
    widths = INTEGER_FORMAT_RANGES if logical == "integer" else _NUMBER_FORMAT_RANGES
    bounds = widths.get(fmt)
    if bounds is None:
        return b.skip(
            b.site.column, "format", f"{logical} format {fmt!r} has no value range to enforce"
        )
    severity: Severity = "error" if b.options.strict_types else "warning"
    return Range(
        **_base(b, prop, "format", "constraint"),
        severity=severity,
        minimum=bounds[0],
        maximum=bounds[1],
    )


def _plan_format(b: _Builder, prop: Property) -> CheckSpec | Skipped | None:
    fmt = prop.options.get("format")
    if fmt is None:
        return None
    logical = (prop.logical_type or "").lower()
    if not isinstance(fmt, str):
        return b.skip(b.site.column, "format", "format is not a text value")
    if logical in ("integer", "number"):
        return _plan_numeric_format(b, prop, fmt, logical)
    if logical == "string" and fmt in _STRING_FORMATS:
        return FormatIs(**_base(b, prop, "format", "constraint"), format=fmt)
    if logical in _DATE_LOGICAL_TYPES and jdk_to_strptime(fmt) is not None:
        return FormatIs(**_base(b, prop, "format", "constraint"), format=fmt)
    return b.skip(b.site.column, "format", f"format {fmt!r} is not evaluated by this engine")


def _plan_items(b: _Builder, prop: Property) -> CheckSpec | Skipped | None:
    lower, upper = prop.options.get("minItems"), prop.options.get("maxItems")
    unique = prop.options.get("uniqueItems")
    if lower is None and upper is None and unique is None:
        return None
    bounds = _whole_bounds(lower, upper)
    if bounds is None or (unique is not None and not isinstance(unique, bool)):
        return b.skip(
            b.site.column,
            "items",
            "minItems, maxItems and uniqueItems must be whole numbers and a boolean",
        )
    return ArrayBounds(
        **_base(b, prop, "items", "constraint"),
        min_items=bounds[0],
        max_items=bounds[1],
        unique_items=unique is True,
    )


# The fixed per-property rule order of the report.
_PROPERTY_PLANNERS: tuple[tuple[str, Planner], ...] = (
    ("present", _plan_present),
    ("type", _plan_type),
    ("physical_type", _plan_physical_type),
    ("required", _plan_required),
    ("unique", _plan_unique),
    ("enum", _plan_enum),
    ("pattern", _plan_pattern),
    ("length", _plan_length),
    ("range", _plan_range),
    ("multiple_of", _plan_multiple_of),
    ("format", _plan_format),
    ("items", _plan_items),
)


def _quality_base(
    b: _Builder, check_id: str, severity: Severity, *, on_property: bool
) -> dict[str, Any]:
    """The fields a quality check carries; object-level rules mask when any property is masked."""
    if not on_property:
        masked = any(map(b.masked, b.obj.properties))
        return {
            "check_id": check_id,
            "category": "quality",
            "rule": "quality",
            "severity": severity,
            "masked": masked,
        }
    site = b.site
    nested = site.depth > 1
    return {
        "check_id": check_id,
        "category": "quality",
        "rule": "quality",
        "column": site.column,
        "physical_name": None if nested else site.prop.physical_name,
        "severity": severity,
        "masked": b.site_masked(),
        "steps": site.steps if nested else (),
    }


def _plan_quality(b: _Builder, rule: QualityRule, *, on_property: bool) -> CheckSpec | Skipped:
    """A library rule becomes a check; anything else is skipped with the reason."""
    column = b.site.column if on_property else None
    scope = b.obj.name if column is None else f"{b.obj.name}.{column}"
    check_id = b.unique_id(rule.id or f"{scope}.quality[{rule.index}]")
    severity = severity_of(rule)
    base = _quality_base(b, check_id, severity, on_property=on_property)
    built = build_quality_check(
        rule,
        base=base,
        has_column=on_property,
        sampled=b.options.sampled,
        names=b.source_names,
    )
    if isinstance(built, str):
        return Skipped(
            check_id=check_id,
            category="quality",
            rule="quality",
            reason=built,
            column=column,
            severity=severity,
        )
    return built


def _plan_site(b: _Builder, site: PropertyPath) -> None:
    """Plan one property or nested path: its rules in the fixed order, then its quality rules."""
    b.site = site
    if site.truncated:
        reason = f"nesting deeper than {b.options.max_depth} levels is not evaluated"
        b.keep(b.skip(site.column, "nesting", reason, "schema"))
        return
    for rule, planner in _PROPERTY_PLANNERS:
        if b.options.schema_only and rule not in _SCHEMA_ONLY_RULES:
            continue
        b.keep(planner(b, site.prop))
    if not b.options.schema_only:
        for quality in site.prop.quality:
            b.keep(_plan_quality(b, quality, on_property=True))


def _key_order(prop: Property) -> tuple[int, int]:
    """Positioned key columns first by primaryKeyPosition, the rest in contract order."""
    position = prop.primary_key_position
    return (0, position) if position is not None and position >= 1 else (1, 0)


def _plan_primary_key(b: _Builder) -> PrimaryKey | None:
    keys = sorted((prop for prop in b.obj.properties if prop.primary_key), key=_key_order)
    if not keys or b.options.schema_only:
        return None
    return PrimaryKey(
        check_id=b.rule_id(None, "primary_key"),
        category="schema",
        rule="primary_key",
        masked=any(b.masked(prop) for prop in keys),
        columns=tuple(prop.source_name for prop in keys),
    )


def _plan_extra(b: _Builder) -> ExtraColumns | None:
    if b.options.extra == "allow":
        return None
    return ExtraColumns(
        check_id=b.rule_id(None, "extra_columns"),
        category="schema",
        rule="extra_columns",
        severity="warning" if b.options.extra == "warn" else "error",
        expected=tuple(prop.source_name for prop in b.obj.properties),
    )


def plan(obj: SchemaObject, options: PlanOptions | None = None) -> Plan:
    """Plan every check for one schema object, in report order."""
    builder = _Builder(obj, options or PlanOptions())
    for site in property_paths(obj, max_depth=builder.options.max_depth):
        _plan_site(builder, site)
    builder.keep(_plan_primary_key(builder))
    builder.keep(_plan_extra(builder))
    if not builder.options.schema_only:
        for quality in obj.quality:
            builder.keep(_plan_quality(builder, quality, on_property=False))
    columns = tuple(dict.fromkeys(prop.source_name for prop in obj.properties))
    return Plan(
        object_name=obj.name,
        checks=tuple(builder.checks),
        skipped=tuple(builder.skipped),
        order=tuple(builder.order),
        referenced_columns=columns,
    )
