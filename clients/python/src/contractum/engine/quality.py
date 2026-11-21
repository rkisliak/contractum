"""Library quality metrics of an ODCS contract, evaluated on Arrow columns.

Only declarative ``library`` rules are evaluated: ``nullValues``, ``missingValues``,
``invalidValues``, ``duplicateValues`` and ``rowCount``, each compared with a threshold through
one of the eight ODCS operators, in rows or in percent. ``text``, ``sql`` and ``custom`` rules,
unknown metrics, rules without an operator or with an unusable threshold are skipped with a
reason and never passed; SQL or code taken from a contract is never executed.

``build_quality_check`` runs at plan time (it returns a check or a skip reason) and
``evaluate_quality`` runs in the backend (it returns a ``QualityResult``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import pyarrow.compute as pc

from contractum.engine import kernels
from contractum.engine.checks import QualityMetric, RowCount, Severity
from contractum.engine.nested import NestedColumn
from contractum.engine.regex import UNSUPPORTED_PATTERN, re2_supported
from contractum.engine.types import real_number, whole_number
from contractum.odcs import QualityRule

_WARNING_SEVERITIES = frozenset({"warning", "warn", "info"})
_UNITS = frozenset({"rows", "percent"})
_PASSED_ON_SAMPLE = "passed on a sample only"

_OPERATORS: dict[str, Callable[[float, Any], bool]] = {
    "mustBe": lambda value, threshold: bool(value == threshold),
    "mustNotBe": lambda value, threshold: bool(value != threshold),
    "mustBeGreaterThan": lambda value, threshold: bool(value > threshold),
    "mustBeGreaterOrEqualTo": lambda value, threshold: bool(value >= threshold),
    "mustBeLessThan": lambda value, threshold: bool(value < threshold),
    "mustBeLessOrEqualTo": lambda value, threshold: bool(value <= threshold),
    "mustBeBetween": lambda value, threshold: bool(threshold[0] <= value <= threshold[1]),
    "mustNotBeBetween": lambda value, threshold: bool(not threshold[0] <= value <= threshold[1]),
}
_BETWEEN = frozenset({"mustBeBetween", "mustNotBeBetween"})
_PHRASES = {
    "mustBe": "equal to",
    "mustNotBe": "not equal to",
    "mustBeGreaterThan": "greater than",
    "mustBeGreaterOrEqualTo": "greater than or equal to",
    "mustBeLessThan": "less than",
    "mustBeLessOrEqualTo": "less than or equal to",
    "mustBeBetween": "between",
    "mustNotBeBetween": "not between",
}
_PROPERTY_METRICS = frozenset({"nullValues", "missingValues", "invalidValues"})
_METRICS = _PROPERTY_METRICS | {"duplicateValues", "rowCount"}


def severity_of(rule: QualityRule) -> Severity:
    """``warning``, ``warn`` and ``info`` make a failed rule a warning; others are errors."""
    label = (rule.severity or "").strip().lower()
    return "warning" if label in _WARNING_SEVERITIES else "error"


def compare(operator: str, observed: float, threshold: Any) -> bool:
    """Whether the observed value satisfies ``operator`` against the threshold."""
    return _OPERATORS[operator](observed, threshold)


# --- plan time --------------------------------------------------------------------------------


def _type_reason(rule: QualityRule) -> str | None:
    kind = (rule.type or "library").lower()
    if kind == "library":
        return None
    if kind == "text":
        return "text rule: documentation only"
    if kind == "sql":
        return "sql rules are not evaluated locally"
    if kind == "custom":
        return f"custom engine rule: {rule.engine or 'unspecified'}"
    return f"quality rule type {kind!r} is not recognised"


def _threshold_reason(operator: str, threshold: Any) -> str | None:
    if operator in _BETWEEN:
        bounds = threshold if isinstance(threshold, (list, tuple)) and len(threshold) == 2 else None
        numbers = [real_number(bound) for bound in bounds] if bounds is not None else []
        if len(numbers) == 2 and None not in numbers and numbers[0] <= numbers[1]:  # type: ignore[operator]
            return None
        return f"{operator} needs two numbers [low, high] with low <= high"
    return None if real_number(threshold) is not None else f"{operator} needs a number threshold"


def _invalid_values_reason(arguments: Mapping[str, Any]) -> str | None:
    valid, regex = arguments.get("validValues"), arguments.get("pattern")
    lower, upper = arguments.get("minLength"), arguments.get("maxLength")
    if valid is None and regex is None and lower is None and upper is None:
        return "invalidValues needs validValues, pattern, minLength or maxLength"
    if valid is not None and not isinstance(valid, list):
        return "validValues must be a list"
    if regex is not None and not isinstance(regex, str):
        return "pattern is not a text value"
    if isinstance(regex, str) and not re2_supported(regex):
        return UNSUPPORTED_PATTERN
    if any(bound is not None and whole_number(bound) is None for bound in (lower, upper)):
        return "minLength and maxLength must be whole numbers"
    return None


def _metric_reason(
    rule: QualityRule, metric: str, *, has_column: bool, sampled: bool
) -> str | None:
    """Why this metric cannot be evaluated where the rule sits, or None."""
    if metric == "rowCount":
        if sampled:
            return "row count of a sample is meaningless"
        if rule.unit == "percent":
            return "rowCount is a count of rows: unit percent does not apply"
        return None
    if metric in _PROPERTY_METRICS and not has_column:
        return f"{metric} needs a property: declare it on the property"
    if metric == "missingValues" and not isinstance(rule.arguments.get("missingValues", []), list):
        return "missingValues must be a list"
    if metric == "invalidValues":
        return _invalid_values_reason(rule.arguments)
    if metric == "duplicateValues" and not has_column:
        keys = rule.arguments.get("properties")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            return "duplicateValues at object level needs arguments.properties"
    return None


def _rule_reason(rule: QualityRule, *, has_column: bool, sampled: bool) -> str | None:
    reason = _type_reason(rule)
    if reason is not None:
        return reason
    metric = rule.metric
    if metric is None:
        return "library rule has no metric"
    if metric not in _METRICS:
        return f"unknown quality metric {metric!r}"
    if rule.operator is None:
        return "library rule has no threshold operator"
    reason = _threshold_reason(rule.operator, rule.threshold)
    if reason is not None:
        return reason
    if rule.unit is not None and rule.unit not in _UNITS:
        return f"unit {rule.unit!r} is not supported (rows or percent)"
    return _metric_reason(rule, metric, has_column=has_column, sampled=sampled)


def _key_names(rule: QualityRule, names: Mapping[str, str]) -> tuple[str, ...] | str:
    """Dataset column names of an object-level ``duplicateValues`` key, or a skip reason."""
    keys: list[str] = []
    for key in rule.arguments["properties"]:
        if key not in names:
            return f"duplicateValues names unknown property {key!r}"
        keys.append(names[key])
    return tuple(keys)


def build_quality_check(
    rule: QualityRule,
    *,
    base: Mapping[str, Any],
    has_column: bool,
    sampled: bool,
    names: Mapping[str, str],
) -> QualityMetric | RowCount | str:
    """The check for a quality rule, or the reason it is skipped.

    ``base`` holds the fields every check carries; ``names`` maps property names (and physical
    names) to dataset column names for an object-level ``duplicateValues`` key.
    """
    reason = _rule_reason(rule, has_column=has_column, sampled=sampled)
    if reason is not None:
        return reason
    metric, operator = str(rule.metric), str(rule.operator)
    if metric == "rowCount":
        return RowCount(**base, operator=operator, threshold=rule.threshold)
    arguments: dict[str, Any] = dict(rule.arguments)
    if metric == "duplicateValues" and not has_column:
        keys = _key_names(rule, names)
        if isinstance(keys, str):
            return keys
        arguments["properties"] = keys
    return QualityMetric(
        **base,
        metric=metric,
        operator=operator,
        threshold=rule.threshold,
        unit=rule.unit or "rows",
        arguments=arguments,
    )


# --- evaluation -------------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityResult:
    """The verdict of one quality rule.

    ``passed`` is None for a skipped rule. ``mask`` marks the rows the metric counted (for failure
    samples), ``failed_rows`` is the metric's row count when the rule failed and 0 when it held.
    """

    passed: bool | None
    observed: Any = None
    threshold: Any = None
    failed_rows: int | None = None
    total_rows: int | None = None
    message: str | None = None
    mask: Any = None
    skipped_reason: str | None = None


@dataclass(frozen=True)
class _Counted:
    count: int
    total: int
    mask: Any = None


Metric = Callable[[Mapping[str, Any], Sequence[NestedColumn]], "_Counted | str"]


def _count(mask: Any) -> int:
    return int(pc.sum(mask).as_py() or 0)


def _within(column: NestedColumn, mask: Any) -> Any:
    """Restrict a row mask to elements whose parent is present."""
    return mask if column.present is None else pc.and_(mask, column.present)


def _null_mask(column: NestedColumn) -> Any:
    return _within(column, pc.is_null(column.values, nan_is_null=True))


def _null_values(_: Mapping[str, Any], columns: Sequence[NestedColumn]) -> _Counted | str:
    mask = _null_mask(columns[0])
    return _Counted(_count(mask), columns[0].total, mask)


def _missing_values(
    arguments: Mapping[str, Any], columns: Sequence[NestedColumn]
) -> _Counted | str:
    column = columns[0]
    mask = _null_mask(column)
    listed = arguments.get("missingValues") or []
    if listed:
        members = kernels.member_mask(column.values, listed)
        if members is None:
            return f"missingValues is not evaluated on {column.values.type} columns"
        mask = pc.or_(mask, _within(column, members))
    return _Counted(_count(mask), column.total, mask)


def _invalid_conditions(arguments: Mapping[str, Any], values: Any) -> list[kernels.KernelResult]:
    results = []
    if arguments.get("validValues") is not None:
        results.append(kernels.in_set(values, arguments["validValues"]))
    if arguments.get("pattern") is not None:
        results.append(kernels.pattern(values, arguments["pattern"]))
    lower, upper = arguments.get("minLength"), arguments.get("maxLength")
    if lower is not None or upper is not None:
        results.append(kernels.length(values, min_length=lower, max_length=upper))
    return results


def _invalid_values(
    arguments: Mapping[str, Any], columns: Sequence[NestedColumn]
) -> _Counted | str:
    column = columns[0]
    mask = None
    for result in _invalid_conditions(arguments, column.values):
        if result.skipped_reason is not None:
            return result.skipped_reason
        mask = result.failed_mask if mask is None else pc.or_(mask, result.failed_mask)
    mask = _within(column, mask)
    return _Counted(_count(mask), column.total, mask)


def _duplicate_values(_: Mapping[str, Any], columns: Sequence[NestedColumn]) -> _Counted | str:
    result = kernels.duplicate_values([column.values for column in columns])
    if result.skipped_reason is not None:
        return result.skipped_reason
    return _Counted(result.failed_rows, columns[0].total, result.failed_mask)


_METRIC_FUNCTIONS: dict[str, Metric] = {
    "nullValues": _null_values,
    "missingValues": _missing_values,
    "invalidValues": _invalid_values,
    "duplicateValues": _duplicate_values,
}


def _skipped(reason: str) -> QualityResult:
    return QualityResult(None, skipped_reason=reason)


def _describe(operator: str, threshold: Any) -> str:
    shown = " and ".join(str(bound) for bound in threshold) if operator in _BETWEEN else threshold
    return f"{_PHRASES[operator]} {shown}"


def _verdict(
    metric: str,
    operator: str,
    threshold: Any,
    unit: str,
    counted: _Counted,
    sampled: bool,
) -> QualityResult:
    if unit == "percent":
        observed: float = round(counted.count * 100 / counted.total, 6) if counted.total else 0.0
        suffix = " percent"
    else:
        observed, suffix = counted.count, ""
    if compare(operator, observed, threshold):
        note = _PASSED_ON_SAMPLE if sampled and metric == "duplicateValues" else None
        return QualityResult(True, observed, threshold, 0, counted.total, note)
    message = f"{metric} is {observed}{suffix}, expected {_describe(operator, threshold)}"
    return QualityResult(
        False, observed, threshold, counted.count, counted.total, message, counted.mask
    )


def evaluate_quality(
    spec: QualityMetric | RowCount,
    columns: Sequence[NestedColumn],
    *,
    rows: int,
    sampled: bool = False,
) -> QualityResult:
    """Evaluate one planned quality check on the columns it names (none for ``rowCount``)."""
    if spec.operator not in _OPERATORS:
        return _skipped("library rule has no threshold operator")
    operator = spec.operator
    reason = _threshold_reason(operator, spec.threshold)
    if reason is not None:
        return _skipped(reason)
    if isinstance(spec, RowCount):
        if sampled:
            return _skipped("row count of a sample is meaningless")
        result = _verdict("rowCount", operator, spec.threshold, "rows", _Counted(rows, rows), False)
        return replace(result, failed_rows=None, total_rows=rows)
    metric = _METRIC_FUNCTIONS.get(spec.metric)
    if metric is None:
        return _skipped(f"unknown quality metric {spec.metric!r}")
    counted_or_reason = metric(spec.arguments, columns)
    if isinstance(counted_or_reason, str):
        return _skipped(counted_or_reason)
    return _verdict(
        spec.metric, operator, spec.threshold, spec.unit or "rows", counted_or_reason, sampled
    )
