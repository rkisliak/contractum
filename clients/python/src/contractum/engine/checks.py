"""The closed set of checks, plans and outcomes.

A contract is turned into frozen ``CheckSpec`` values by ``plan()``; a backend evaluates them into
``CheckOutcome`` values. Anything the engine cannot evaluate is a ``Skipped`` entry with a reason,
never a pass. Pushdown backends (Spark, SQL) receive these same specs (D-32).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["passed", "failed", "warning", "skipped", "error"]
Severity = Literal["error", "warning"]
Category = Literal["schema", "constraint", "quality", "lifecycle"]
ExtraPolicy = Literal["allow", "warn", "fail"]


@dataclass(frozen=True, kw_only=True)
class _Check:
    """Fields every check carries.

    ``column`` is the contract property name, ``physical_name`` its physicalName when the dataset
    column is named differently. ``masked`` hides sample values of sensitive columns. ``steps`` is
    empty for a top-level property; for a nested one it is the dataset path from the top-level
    column (a field name per struct field, None where list items are flattened), and ``column``
    is then the reported path such as ``shipping.city`` or ``tags[]``.
    """

    check_id: str
    category: Category
    rule: str
    column: str | None = None
    physical_name: str | None = None
    severity: Severity = "error"
    masked: bool = False
    steps: tuple[str | None, ...] = ()

    @property
    def source_column(self) -> str | None:
        """The dataset column the check reads: physicalName else name."""
        return self.physical_name or self.column


@dataclass(frozen=True, kw_only=True)
class ColumnPresent(_Check):
    """The column exists in the dataset."""


@dataclass(frozen=True, kw_only=True)
class ExtraColumns(_Check):
    """The dataset has no columns beyond ``expected`` (dataset column names)."""

    expected: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class TypeIs(_Check):
    """The column holds values of a logical type."""

    logical_type: str
    strict: bool = False


@dataclass(frozen=True, kw_only=True)
class PhysicalTypeIs(_Check):
    """The column has a named physical type."""

    physical_type: str


@dataclass(frozen=True, kw_only=True)
class NotNull(_Check):
    """The column has no nulls."""


@dataclass(frozen=True, kw_only=True)
class Unique(_Check):
    """Non-null values of the column are unique."""


@dataclass(frozen=True, kw_only=True)
class PrimaryKey(_Check):
    """The key columns (dataset names, in key order) are never null and jointly unique."""

    columns: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class InSet(_Check):
    """Non-null values belong to ``values``."""

    values: tuple[Any, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Pattern(_Check):
    """Non-null string values match a regular expression."""

    pattern: str


@dataclass(frozen=True, kw_only=True)
class Length(_Check):
    """String length bounds."""

    min_length: int | None = None
    max_length: int | None = None


@dataclass(frozen=True, kw_only=True)
class Range(_Check):
    """Numeric or temporal bounds."""

    minimum: Any = None
    maximum: Any = None
    exclusive_minimum: Any = None
    exclusive_maximum: Any = None


@dataclass(frozen=True, kw_only=True)
class MultipleOf(_Check):
    """Numeric values are multiples of ``factor``."""

    factor: float


@dataclass(frozen=True, kw_only=True)
class FormatIs(_Check):
    """Values follow a named format."""

    format: str


@dataclass(frozen=True, kw_only=True)
class ArrayBounds(_Check):
    """Array length bounds and element uniqueness."""

    min_items: int | None = None
    max_items: int | None = None
    unique_items: bool = False


@dataclass(frozen=True, kw_only=True)
class QualityMetric(_Check):
    """A library quality metric compared to a threshold."""

    metric: str
    operator: str | None = None
    threshold: Any = None
    unit: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class RowCount(_Check):
    """The row count compared to a threshold."""

    operator: str | None = None
    threshold: Any = None


CheckSpec = (
    ColumnPresent
    | ExtraColumns
    | TypeIs
    | PhysicalTypeIs
    | NotNull
    | Unique
    | PrimaryKey
    | InSet
    | Pattern
    | Length
    | Range
    | MultipleOf
    | FormatIs
    | ArrayBounds
    | QualityMetric
    | RowCount
)


@dataclass(frozen=True)
class Skipped:
    """A check the engine could not evaluate, with the reason the report prints."""

    check_id: str
    category: Category
    rule: str
    reason: str
    column: str | None = None
    severity: Severity = "error"


@dataclass(frozen=True)
class Plan:
    """The checks for one schema object.

    ``order`` lists every check id, evaluated or skipped, in report order;
    ``referenced_columns`` are the dataset columns the checks read (for column projection).
    """

    object_name: str
    checks: tuple[CheckSpec, ...]
    skipped: tuple[Skipped, ...]
    order: tuple[str, ...]
    referenced_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sample:
    """One failing position (0-based) and its value, masked when the column is sensitive."""

    row: int
    value: Any = None


@dataclass(frozen=True)
class CheckOutcome:
    """The result of one check on a backend."""

    check_id: str
    category: Category
    column: str | None
    rule: str
    status: Status
    severity: Severity = "error"
    message: str | None = None
    failed_rows: int | None = None
    total_rows: int | None = None
    observed: Any = None
    threshold: Any = None
    samples: tuple[Sample, ...] = ()
    skipped_reason: str | None = None
    sampled: bool = False
