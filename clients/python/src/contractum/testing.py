"""Test a dataset against a contract: ``run_test``.

The pipeline is load -> sample -> coerce -> plan -> evaluate -> lifecycle -> report. Everything
happens in the caller's process; no dataset value, sample or result is sent anywhere (D-20).
``run_test`` keeps no module-level mutable state, so several threads can test different datasets
at the same time.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, field_validator

import contractum
from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import RunOptions
from contractum.engine.checks import CheckOutcome, CheckSpec, Plan, Skipped
from contractum.engine.lifecycle import lifecycle_outcome
from contractum.engine.masking import MaskMode
from contractum.engine.plan import PlanOptions, plan
from contractum.engine.sampling import apply_sampling
from contractum.errors import UsageError
from contractum.odcs import SchemaObject, find_object, load_document
from contractum.report.model import ContractInfo, DatasetInfo, Report, ToolInfo, build_report
from contractum.sources import LoadedDataset, load_dataset
from contractum.sources.coerce import coerce_to_contract

SCHEMA_ONLY_REASON = "schema-only run"
_SCHEMA_ONLY_RULES = frozenset({"present", "type", "physical_type", "extra_columns"})

_SUFFIX_MEDIA = {
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


class TestOptions(BaseModel):
    """How a test run reads, samples and judges a dataset (frozen and validated)."""

    __test__: ClassVar[bool] = False

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    object_name: str | None = None
    extra: Literal["allow", "warn", "fail"] = "warn"
    ignore_case: bool = False
    strict_types: bool = False
    sample: int | float | None = None
    seed: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1)
    max_failures: int = Field(default=5, ge=0)
    mask: MaskMode = "auto"
    samples: bool = True
    strict: bool = False
    fail_on_skipped: bool = False
    fail_on_deprecated: bool = False
    sunset_within_days: int | None = Field(default=None, ge=0)
    schema_only: bool = False
    format: str | None = None
    dialect: str | None = None

    @field_validator("sample")
    @classmethod
    def _valid_sample(cls, value: int | float | None) -> int | float | None:
        if value is None:
            return value
        if isinstance(value, float):
            if not 0 < value <= 1:
                raise ValueError("a sample fraction must be above 0 and at most 1")
        elif value < 1:
            raise ValueError("a sample row count must be at least 1")
        return value


def run_test(
    contract: Any,
    data: object,
    *,
    options: TestOptions | None = None,
    contract_info: ContractInfo | None = None,
    today: date | None = None,
) -> Report:
    """Test ``data`` against one schema object of ``contract`` and return the report.

    ``contract`` is a document mapping, YAML or JSON text or bytes, a file path, or any object
    with ``document`` and ``info`` attributes (a fetched ``ContractVersion``). ``data`` is a
    pandas, polars or pyarrow object, an Arrow stream, or a path to a Parquet, CSV, TSV, JSON,
    NDJSON file or a Delta table.
    """
    opts = options or TestOptions()
    started = datetime.now(UTC)
    clock = time.monotonic()
    document, info = _contract_of(contract, contract_info)
    obj = find_object(document, opts.object_name)
    loaded = load_dataset(
        data,
        format=opts.format,
        columns=plan(obj, _plan_options(opts, sampled=False)).referenced_columns,
        limit=opts.limit,
        ignore_case=opts.ignore_case,
        schema_only=opts.schema_only,
        dialect=opts.dialect,
        object_name=_object_names(obj),
    )
    run = _schema_run(obj, opts, loaded) if loaded.schema_only else _data_run(obj, opts, loaded)
    lifecycle = lifecycle_outcome(
        deprecated=info.deprecated,
        sunset_date=info.sunset_date,
        today=today or date.today(),
        fail_on_deprecated=opts.fail_on_deprecated,
        sunset_within_days=opts.sunset_within_days,
    )
    return build_report(
        tool=ToolInfo(version=contractum.__version__),
        contract=info,
        dataset=DatasetInfo(
            kind=loaded.kind,
            location=loaded.location,
            rows_total=run.rows_total,
            rows_tested=run.rows_tested,
            sampled=run.sampled,
            limit=None if loaded.schema_only else opts.limit,
            schema_only=loaded.schema_only,
        ),
        plan=run.plan,
        outcomes=run.outcomes,
        lifecycle=lifecycle,
        started_at=started,
        duration_ms=int((time.monotonic() - clock) * 1000),
    )


@dataclasses.dataclass(frozen=True)
class _Run:
    """What evaluating one dataset produced, ready for the report."""

    plan: Plan
    outcomes: list[CheckOutcome]
    rows_total: int | None
    rows_tested: int
    sampled: bool


def _data_run(obj: SchemaObject, opts: TestOptions, loaded: LoadedDataset) -> _Run:
    table, sampling = apply_sampling(
        _with_unread_columns(loaded, opts), sample=opts.sample, seed=opts.seed, limit=opts.limit
    )
    table, failures = coerce_to_contract(
        table,
        obj,
        text_columns=loaded.text_columns,
        ignore_case=opts.ignore_case,
        max_samples=opts.max_failures,
    )
    if loaded.rows_total is not None:
        sampling = dataclasses.replace(sampling, rows_total=loaded.rows_total)
    the_plan = plan(obj, _plan_options(opts, sampled=sampling.sampled))
    backend = ArrowBackend.from_sampling(table, sampling, parse_failures=failures)
    outcomes = backend.evaluate(the_plan.checks, _run_options(opts))
    return _Run(the_plan, outcomes, loaded.rows_total, table.num_rows, sampling.sampled)


def _schema_run(obj: SchemaObject, opts: TestOptions, loaded: LoadedDataset) -> _Run:
    """Judge only the column names and types of a schema; every value check is skipped."""
    the_plan = _schema_only_plan(obj, opts)
    outcomes = ArrowBackend.from_schema(loaded.schema).evaluate(the_plan.checks, _run_options(opts))
    return _Run(the_plan, outcomes, None, 0, False)


def _schema_only_plan(obj: SchemaObject, opts: TestOptions) -> Plan:
    """The full plan with each check that needs rows turned into a skipped one.

    The report then lists every check of the contract, so a reader sees what a schema-only run
    did not look at instead of a shorter list.
    """
    full = plan(obj, _plan_options(opts, sampled=False, schema_only=False))
    kept = tuple(check for check in full.checks if check.rule in _SCHEMA_ONLY_RULES)
    skipped = {entry.check_id: _schema_only_skip(entry) for entry in full.skipped}
    for check in full.checks:
        if check.rule not in _SCHEMA_ONLY_RULES:
            skipped[check.check_id] = _schema_only_skip(check)
    return dataclasses.replace(full, checks=kept, skipped=tuple(skipped.values()))


def _schema_only_skip(entry: CheckSpec | Skipped) -> Skipped:
    if isinstance(entry, Skipped) and entry.category == "schema":
        return entry
    return Skipped(
        check_id=entry.check_id,
        category=entry.category,
        rule=entry.rule,
        reason=SCHEMA_ONLY_REASON,
        column=entry.column,
        severity=entry.severity,
    )


def _run_options(opts: TestOptions) -> RunOptions:
    return RunOptions(
        max_failures=opts.max_failures, samples=opts.samples, ignore_case=opts.ignore_case
    )


def _object_names(obj: SchemaObject) -> tuple[str, ...]:
    """The names a SQL table may carry to be this schema object: its name and physicalName."""
    return tuple(name for name in (obj.name, obj.physical_name) if name)


def _plan_options(opts: TestOptions, *, sampled: bool, schema_only: bool = False) -> PlanOptions:
    return PlanOptions(
        extra=opts.extra,
        strict_types=opts.strict_types,
        mask=opts.mask,
        sampled=sampled,
        schema_only=schema_only,
    )


def _with_unread_columns(loaded: LoadedDataset, opts: TestOptions) -> pa.Table:
    """The table, plus an all-null placeholder for each column a reader skipped.

    Only the extra-columns check looks at a column nobody asked for, and it only needs the name,
    so skipped columns of the file are added as zero-cost null columns unless extras are allowed.
    """
    table = loaded.table
    if opts.extra == "allow":
        return table
    for name in loaded.schema.names:
        if name not in table.column_names:
            table = table.append_column(name, pa.nulls(table.num_rows))
    return table


def _contract_of(
    contract: Any, override: ContractInfo | None
) -> tuple[Mapping[str, Any], ContractInfo]:
    """The document and the contract block of the report for any accepted contract input."""
    if hasattr(contract, "document") and hasattr(contract, "info"):
        return contract.document, override or contract.info
    raw = _raw_contract(contract)
    if isinstance(raw, Mapping):
        return raw, override or _info_of(raw, None)
    document = load_document(raw[0], media_type=raw[1])
    return document, override or _info_of(document, hashlib.sha256(raw[0]).hexdigest())


def _raw_contract(contract: Any) -> Mapping[str, Any] | tuple[bytes, str | None]:
    if isinstance(contract, Mapping):
        return contract
    if isinstance(contract, (bytes, bytearray)):
        return bytes(contract), None
    if isinstance(contract, os.PathLike) or (isinstance(contract, str) and _is_file(contract)):
        return _read_contract_file(Path(contract))
    if isinstance(contract, str):
        return contract.encode("utf-8"), None
    raise UsageError(f"cannot use a {type(contract).__name__} as a contract")


def _is_file(text: str) -> bool:
    return "\n" not in text and len(text) < 4096 and Path(text).is_file()


def _read_contract_file(path: Path) -> tuple[bytes, str | None]:
    try:
        return path.read_bytes(), _SUFFIX_MEDIA.get(path.suffix.lower())
    except OSError as exc:
        raise UsageError(f"cannot read {path.name}: {type(exc).__name__}") from None


def _info_of(document: Mapping[str, Any], digest: str | None) -> ContractInfo:
    def text(key: str) -> str | None:
        value = document.get(key)
        return value if isinstance(value, str) and value else None

    return ContractInfo(
        source="file",
        odcs_id=text("id"),
        name=text("name"),
        semver=text("version"),
        status=text("status"),
        content_sha256=digest,
    )


__all__ = ["TestOptions", "run_test"]
