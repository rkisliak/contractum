"""The first backend: checks evaluated on a pyarrow table.

Kernels are small methods keyed by check type. A kernel that raises becomes an ``error`` outcome
carrying only the exception type name, because Arrow messages can contain data values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from contractum.engine import kernels
from contractum.engine.backend import ColumnInfo, DatasetSchema, ParseFailure, RunOptions
from contractum.engine.checks import (
    ArrayBounds,
    CheckOutcome,
    CheckSpec,
    ColumnPresent,
    ExtraColumns,
    FormatIs,
    InSet,
    Length,
    MultipleOf,
    NotNull,
    Pattern,
    PhysicalTypeIs,
    PrimaryKey,
    QualityMetric,
    Range,
    RowCount,
    Sample,
    Status,
    TypeIs,
    Unique,
)
from contractum.engine.kernels import KernelResult
from contractum.engine.masking import render_value
from contractum.engine.nested import Missing, NestedColumn, NestedView
from contractum.engine.normalise import normalise
from contractum.engine.quality import evaluate_quality
from contractum.engine.sampling import SamplingInfo
from contractum.engine.types import logical_accepts, physical_type_accepts

_MAX_LISTED_COLUMNS = 10
_COLUMN_MISSING = "column is missing from the dataset"


def _error_message(exc: Exception) -> str:
    """The only text an engine failure may carry: the exception type, never its message."""
    return f"check could not be evaluated: {type(exc).__name__}"


# Check type -> how to bind the spec's parameters to the kernel that judges one column.
_COLUMN_KERNELS: dict[type, Callable[[Any], Callable[[Any], KernelResult]]] = {
    Unique: lambda spec: kernels.unique,
    InSet: lambda spec: partial(kernels.in_set, values=spec.values),
    Pattern: lambda spec: partial(kernels.pattern, regex=spec.pattern),
    Length: lambda spec: partial(
        kernels.length, min_length=spec.min_length, max_length=spec.max_length
    ),
    Range: lambda spec: partial(
        kernels.range_check,
        minimum=spec.minimum,
        maximum=spec.maximum,
        exclusive_minimum=spec.exclusive_minimum,
        exclusive_maximum=spec.exclusive_maximum,
    ),
    MultipleOf: lambda spec: partial(kernels.multiple_of, factor=spec.factor),
    FormatIs: lambda spec: partial(kernels.format_is, fmt=spec.format),
    ArrayBounds: lambda spec: partial(
        kernels.array_bounds,
        min_items=spec.min_items,
        max_items=spec.max_items,
        unique_items=spec.unique_items,
    ),
}


class ArrowBackend:
    """Evaluates checks on one pyarrow table.

    ``rows_total`` is the size of the source before a limit or sample, ``row_index`` maps table
    positions to source row numbers when the table is a subset, and ``parse_failures`` carries,
    for each column that was read as text and coerced, the values that did not parse (one entry per
    coerced column, even with count 0).
    """

    name = "arrow"

    def __init__(
        self,
        table: pa.Table,
        *,
        rows_total: int | None = None,
        sampled: bool = False,
        row_index: Sequence[int] | None = None,
        parse_failures: Mapping[str, ParseFailure] | None = None,
    ) -> None:
        self._source_types = {field.name: field.type for field in table.schema}
        self._table = normalise(table)
        self._view = NestedView(self._table)
        self.rows_total = table.num_rows if rows_total is None else rows_total
        self.sampled = sampled
        self._row_index = row_index
        self._parse_failures = dict(parse_failures or {})
        self._kernels: dict[type, Callable[[Any, RunOptions], CheckOutcome]] = {
            ColumnPresent: self._present,
            ExtraColumns: self._extra_columns,
            TypeIs: self._type_is,
            PhysicalTypeIs: self._physical_type,
            NotNull: self._not_null,
            PrimaryKey: self._primary_key,
            QualityMetric: self._quality,
            RowCount: self._quality,
        }
        for spec_type, bind in _COLUMN_KERNELS.items():
            self._kernels[spec_type] = partial(self._on_column, bind)

    @classmethod
    def from_schema(cls, schema: pa.Schema) -> ArrowBackend:
        """A backend over an empty table of the given schema (schema-only runs)."""
        return cls(schema.empty_table())

    @classmethod
    def from_sampling(
        cls,
        table: pa.Table,
        info: SamplingInfo,
        *,
        parse_failures: Mapping[str, ParseFailure] | None = None,
    ) -> ArrowBackend:
        """A backend over a limited or sampled table, citing source row positions."""
        return cls(
            table,
            rows_total=info.rows_total,
            sampled=info.sampled,
            row_index=info.row_index,
            parse_failures=parse_failures,
        )

    @property
    def rows_tested(self) -> int:
        return int(self._table.num_rows)

    def schema(self) -> DatasetSchema:
        return DatasetSchema(
            tuple(ColumnInfo(name, str(dtype)) for name, dtype in self._source_types.items())
        )

    def evaluate(self, checks: Sequence[CheckSpec], opts: RunOptions) -> list[CheckOutcome]:
        outcomes: list[CheckOutcome] = []
        for spec in checks:
            kernel = self._kernels.get(type(spec))
            if kernel is None:
                reason = f"{spec.rule} checks are not evaluated by the arrow backend yet"
                outcomes.append(self._skipped(spec, reason))
                continue
            try:
                outcomes.append(kernel(spec, opts))
            except Exception as exc:  # noqa: BLE001 - any kernel failure must become an outcome
                outcomes.append(self._outcome(spec, "error", message=_error_message(exc)))
        return outcomes

    def _outcome(self, spec: CheckSpec, status: Status, **fields: Any) -> CheckOutcome:
        return CheckOutcome(
            check_id=spec.check_id,
            category=spec.category,
            column=spec.column,
            rule=spec.rule,
            status=status,
            severity=spec.severity,
            sampled=self.sampled,
            **fields,
        )

    def _skipped(self, spec: CheckSpec, reason: str) -> CheckOutcome:
        return self._outcome(spec, "skipped", skipped_reason=reason)

    def _failed(self, spec: CheckSpec, **fields: Any) -> CheckOutcome:
        status: Status = "warning" if spec.severity == "warning" else "failed"
        return self._outcome(spec, status, **fields)

    def _resolve(self, spec: CheckSpec, opts: RunOptions) -> str | None:
        return self._resolve_name(spec.source_column, opts)

    def _resolve_name(self, wanted: str | None, opts: RunOptions) -> str | None:
        names = self._table.column_names
        if wanted is None or wanted in names:
            return wanted
        if opts.ignore_case:
            return next((name for name in names if name.lower() == wanted.lower()), None)
        return None

    def _locate(self, spec: CheckSpec, opts: RunOptions) -> NestedColumn | Missing:
        """The values a check judges: a top-level column or a nested path, else why not."""
        if spec.steps:
            return self._view.column(spec.steps, ignore_case=opts.ignore_case)
        name = self._resolve(spec, opts)
        if name is None:
            return Missing(_COLUMN_MISSING)
        return NestedColumn(self._table[name], source_type=self._source_types[name])

    def _row(self, position: int) -> int:
        return int(self._row_index[position]) if self._row_index is not None else position

    def _sample(self, row: int, value: Any, dtype: Any, masked: bool) -> Sample:
        return Sample(self._row(row), self._shown(value, dtype, masked))

    @staticmethod
    def _shown(value: Any, dtype: Any, masked: bool) -> str | None:
        """The text a sample carries: null stays null, anything else is rendered (and masked)."""
        return None if value is None else render_value(value, dtype, masked=masked)

    def _present(self, spec: ColumnPresent, opts: RunOptions) -> CheckOutcome:
        located = self._locate(spec, opts)
        if not isinstance(located, Missing):
            return self._outcome(spec, "passed")
        if spec.steps:
            if located.absent:
                return self._failed(spec, message=located.reason)
            return self._skipped(spec, located.reason)
        return self._failed(
            spec, message=f"column {spec.source_column!r} is missing from the dataset"
        )

    def _extra_columns(self, spec: ExtraColumns, opts: RunOptions) -> CheckOutcome:
        fold: Callable[[str], str] = str.lower if opts.ignore_case else str
        expected = {fold(name) for name in spec.expected}
        extras = [name for name in self._table.column_names if fold(name) not in expected]
        if not extras:
            return self._outcome(spec, "passed", observed=0, threshold=0)
        listed = ", ".join(extras[:_MAX_LISTED_COLUMNS])
        if len(extras) > _MAX_LISTED_COLUMNS:
            listed += ", ..."
        return self._failed(
            spec,
            message=f"{len(extras)} columns are not in the contract: {listed}",
            observed=len(extras),
            threshold=0,
        )

    def _type_is(self, spec: TypeIs, opts: RunOptions) -> CheckOutcome:
        located = self._locate(spec, opts)
        if isinstance(located, Missing):
            return self._skipped(spec, located.reason)
        failure = None if spec.steps else self._parse_failures.get(self._resolve(spec, opts) or "")
        if failure is not None:
            return self._parse_type(spec, failure, opts)
        dtype = located.dtype
        if logical_accepts(spec.logical_type, dtype, strict_types=spec.strict):
            return self._outcome(spec, "passed", observed=str(dtype), threshold=spec.logical_type)
        return self._failed(
            spec,
            message=f"column type {dtype} does not hold {spec.logical_type} values",
            failed_rows=located.total,
            total_rows=located.total,
            observed=str(dtype),
            threshold=spec.logical_type,
        )

    def _physical_type(self, spec: PhysicalTypeIs, opts: RunOptions) -> CheckOutcome:
        located = self._locate(spec, opts)
        if isinstance(located, Missing):
            return self._skipped(spec, located.reason)
        actual = located.dtype
        accepted = physical_type_accepts(spec.physical_type, actual)
        if accepted is None:
            return self._skipped(spec, f"physicalType {spec.physical_type!r} has no Arrow mapping")
        if accepted:
            return self._outcome(spec, "passed", observed=str(actual), threshold=spec.physical_type)
        return self._failed(
            spec,
            message=f"column type {actual} is not {spec.physical_type}",
            failed_rows=located.total,
            total_rows=located.total,
            observed=str(actual),
            threshold=spec.physical_type,
        )

    def _parse_type(self, spec: TypeIs, failure: ParseFailure, opts: RunOptions) -> CheckOutcome:
        total = self.rows_tested
        if failure.count == 0:
            return self._outcome(
                spec, "passed", failed_rows=0, total_rows=total, threshold=spec.logical_type
            )
        samples: tuple[Sample, ...] = ()
        if opts.samples:
            pairs = list(zip(failure.rows, failure.values, strict=False))[: opts.max_failures]
            samples = tuple(
                self._sample(row, value, pa.large_string(), spec.masked) for row, value in pairs
            )
        return self._failed(
            spec,
            message=f"{failure.count} values are not parseable as {spec.logical_type}",
            failed_rows=failure.count,
            total_rows=total,
            threshold=spec.logical_type,
            samples=samples,
        )

    def _not_null(self, spec: NotNull, opts: RunOptions) -> CheckOutcome:
        located = self._locate(spec, opts)
        if isinstance(located, Missing):
            return self._skipped(spec, located.reason)
        result = kernels.not_null(located.values, located.present)
        return self._finish(spec, result, [located], opts)

    def _finish(
        self,
        spec: CheckSpec,
        result: KernelResult,
        columns: Sequence[NestedColumn],
        opts: RunOptions,
    ) -> CheckOutcome:
        """Turn a kernel result into an outcome with bounded, masked failure samples."""
        if result.skipped_reason is not None:
            return self._skipped(spec, result.skipped_reason)
        fields: dict[str, Any] = {
            "failed_rows": result.failed_rows,
            "total_rows": result.total_rows,
            "observed": result.observed,
            "threshold": result.threshold,
        }
        if result.failed_rows == 0:
            return self._outcome(spec, "passed", **fields)
        return self._failed(
            spec,
            message=result.message,
            samples=self._failure_samples(spec, result.failed_mask, columns, opts),
            **fields,
        )

    def _failure_samples(
        self, spec: CheckSpec, mask: Any, columns: Sequence[NestedColumn], opts: RunOptions
    ) -> tuple[Sample, ...]:
        if not opts.samples or mask is None:
            return ()
        positions = pc.indices_nonzero(mask)[: opts.max_failures].to_pylist()
        first = columns[0]
        if len(columns) == 1:
            values = first.values
            return tuple(
                self._sample(first.top_row(p), values[p].as_py(), values.type, spec.masked)
                for p in positions
            )
        return tuple(
            Sample(
                self._row(p),
                [
                    self._shown(column.values[p].as_py(), column.values.type, spec.masked)
                    for column in columns
                ],
            )
            for p in positions
        )

    def _on_column(
        self,
        bind: Callable[[Any], Callable[[Any], KernelResult]],
        spec: CheckSpec,
        opts: RunOptions,
    ) -> CheckOutcome:
        located = self._locate(spec, opts)
        if isinstance(located, Missing):
            return self._skipped(spec, located.reason)
        return self._finish(spec, bind(spec)(located.values), [located], opts)

    def _primary_key(self, spec: PrimaryKey, opts: RunOptions) -> CheckOutcome:
        names = [self._resolve_name(name, opts) for name in spec.columns]
        gone = [want for want, name in zip(spec.columns, names, strict=True) if name is None]
        if gone:
            return self._skipped(spec, f"key column {gone[0]!r} is missing from the dataset")
        columns = [NestedColumn(self._table[name]) for name in names if name is not None]
        result = kernels.primary_key([column.values for column in columns])
        return self._finish(spec, result, columns, opts)

    def _quality(self, spec: QualityMetric | RowCount, opts: RunOptions) -> CheckOutcome:
        if isinstance(spec, RowCount):
            return self._quality_outcome(spec, [], opts)
        if spec.column is not None:
            located = self._locate(spec, opts)
            if isinstance(located, Missing):
                return self._skipped(spec, located.reason)
            return self._quality_outcome(spec, [located], opts)
        wanted = spec.arguments.get("properties") or ()
        resolved = [(name, self._resolve_name(name, opts)) for name in wanted]
        gone = next((name for name, found in resolved if found is None), None)
        if gone is not None:
            return self._skipped(spec, f"column {gone!r} is missing from the dataset")
        columns = [
            NestedColumn(self._table[found], source_type=self._source_types[found])
            for _, found in resolved
            if found is not None
        ]
        return self._quality_outcome(spec, columns, opts)

    def _quality_outcome(
        self, spec: QualityMetric | RowCount, columns: list[NestedColumn], opts: RunOptions
    ) -> CheckOutcome:
        result = evaluate_quality(spec, columns, rows=self.rows_tested, sampled=self.sampled)
        if result.skipped_reason is not None:
            return self._skipped(spec, result.skipped_reason)
        fields: dict[str, Any] = {
            "failed_rows": result.failed_rows,
            "total_rows": result.total_rows,
            "observed": result.observed,
            "threshold": result.threshold,
            "message": result.message,
        }
        if result.passed:
            return self._outcome(spec, "passed", **fields)
        samples = self._failure_samples(spec, result.mask, columns, opts)
        return self._failed(spec, samples=samples, **fields)
