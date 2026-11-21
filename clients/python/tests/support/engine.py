"""Run a few contract properties against a pyarrow table through plan and ArrowBackend."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
from support.odcs import contract_doc

from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import RunOptions
from contractum.engine.checks import CheckOutcome, Plan
from contractum.engine.plan import PlanOptions, plan
from contractum.odcs import find_object


def prop(name: str, logical: str | None = None, **fields: Any) -> dict[str, Any]:
    """A contract property; keyword fields are ODCS keys such as ``required`` or ``enum``."""
    result: dict[str, Any] = {"name": name, **fields}
    if logical is not None:
        result["logicalType"] = logical
    return result


def options(name: str, logical: str, **constraints: Any) -> dict[str, Any]:
    """A property whose keyword arguments are ``logicalTypeOptions``."""
    return prop(name, logical, logicalTypeOptions=constraints)


def plan_properties(properties: list[dict[str, Any]], **plan_options: Any) -> Plan:
    """The plan for an object ``t`` with the given properties."""
    document = contract_doc(schema=[{"name": "t", "properties": properties}])
    return plan(find_object(document), PlanOptions(**plan_options))


def run_properties(
    properties: list[dict[str, Any]],
    table: pa.Table,
    *,
    run: RunOptions | None = None,
    **plan_options: Any,
) -> dict[str, CheckOutcome]:
    """Outcomes by check id for the object ``t``."""
    the_plan = plan_properties(properties, **plan_options)
    outcomes = ArrowBackend(table).evaluate(the_plan.checks, run or RunOptions())
    return {outcome.check_id: outcome for outcome in outcomes}
