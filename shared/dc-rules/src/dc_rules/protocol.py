"""Wire models of the worker protocol (pure pydantic, no engine import).

The validation web process and the worker subprocess speak JSON lines over stdin and stdout. The
parent writes one job per line. The worker answers with a ``WorkerReady`` line at start, and per
job: ``{"start": id}`` before each unit, ``{"done": id, "result": UnitResult}`` after it and
``{"end": true}`` at the end of the job. A parent that reads a ``start`` marker but no matching
``done`` marker before its deadline knows exactly which unit hung.

This module is imported by both sides, so it never imports the CEL engine, the targeter or the
evaluator.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dc_rules.model import DSL_VERSION, DslVersion, RuleDef
from dc_rules.ports import ErrorCode, Limits

JsonValue = Any

UnitOutcome = Literal["pass", "fail", "error", "invalid"]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class LimitsWire(_Wire):
    """Mirrors `dc_rules.ports.Limits` field for field (including the defaults)."""

    max_expression_bytes: int = 2048
    max_target_chars: int = 512
    max_rules_per_set: int = 200
    max_compiled_bytes: int = 32768
    max_matches: int = 10_000
    max_matches_with_contract: int = 100
    max_fixtures_per_kind: int = 5
    max_fixture_bytes: int = 65536

    @classmethod
    def from_limits(cls, limits: Limits) -> LimitsWire:
        return cls(
            max_expression_bytes=limits.max_expression_bytes,
            max_target_chars=limits.max_target_chars,
            max_rules_per_set=limits.max_rules_per_set,
            max_compiled_bytes=limits.max_compiled_bytes,
            max_matches=limits.max_matches,
            max_matches_with_contract=limits.max_matches_with_contract,
            max_fixtures_per_kind=limits.max_fixtures_per_kind,
            max_fixture_bytes=limits.max_fixture_bytes,
        )

    def to_limits(self) -> Limits:
        return Limits(
            max_expression_bytes=self.max_expression_bytes,
            max_target_chars=self.max_target_chars,
            max_rules_per_set=self.max_rules_per_set,
            max_compiled_bytes=self.max_compiled_bytes,
            max_matches=self.max_matches,
            max_matches_with_contract=self.max_matches_with_contract,
            max_fixtures_per_kind=self.max_fixtures_per_kind,
            max_fixture_bytes=self.max_fixture_bytes,
        )


class FindingWire(_Wire):
    outcome: Literal["fail", "error"]
    pointer: str
    path: str
    error_code: ErrorCode | None = None


class EvalUnit(_Wire):
    """One rule to evaluate. ``cache_key`` is set only for a published version
    (``"<rule_set_version_id>:<rule_key>"``); draft checks leave it ``None`` and are never cached.
    """

    id: int
    cache_key: str | None = None
    rule: RuleDef
    dsl_version: DslVersion = "cel-1"


class CheckUnit(_Wire):
    """One authoring check (answered by the check op, Plan 04)."""

    id: int
    rule: RuleDef
    kind: Literal["compile", "fixture", "probe"]
    fixture_kind: Literal["pass", "fail"] | None = None
    fixture_index: int | None = None
    dsl_version: DslVersion = "cel-1"
    # cel-2 bindings of a fixture unit. A probe unit leaves them empty: the worker builds the
    # synthetic probe bindings itself so the 10,000-property document never crosses the wire.
    previous: JsonValue | None = None
    changes: list[JsonValue] = Field(default_factory=list)
    refs: dict[str, JsonValue] = Field(default_factory=dict)
    # True when the fixture has a baseline but the caller supplied no `changes_for`.
    changes_unavailable: bool = False


class EvaluateJob(_Wire):
    op: Literal["evaluate"] = "evaluate"
    units: list[EvalUnit]
    document: JsonValue
    ctx: dict[str, JsonValue] = Field(default_factory=dict)
    limits: LimitsWire = Field(default_factory=LimitsWire)
    # cel-2 bindings, computed once per job by the caller (D-152, D-153). `previous` is the raw
    # baseline document in the same shape as `document`, or None on a first publish; `changes` is
    # the classified change list (always a list); `refs` the tri-state reference maps.
    previous: JsonValue | None = None
    changes: list[JsonValue] = Field(default_factory=list)
    refs: dict[str, JsonValue] = Field(default_factory=dict)


class CheckJob(_Wire):
    op: Literal["check"] = "check"
    units: list[CheckUnit]
    limits: LimitsWire = Field(default_factory=LimitsWire)


Job = Annotated[EvaluateJob | CheckJob, Field(discriminator="op")]


class UnitResult(_Wire):
    """The answer for one unit.

    ``pass``/``fail``/``error`` come from evaluation; ``invalid`` is a check problem
    (``problem_code``, ``detail`` is author feedback only). An ``error`` result always carries the
    unit-level code in ``error_code`` and, for evaluation, one root-level error finding, so a
    consumer reads errors the same way whether they came from the evaluator or from the sandbox.
    """

    id: int
    outcome: UnitOutcome
    findings: list[FindingWire] = Field(default_factory=list)
    matched: int = 0
    error_code: ErrorCode | None = None
    problem_code: str | None = None
    detail: str | None = None
    compiled_bytes: int | None = None


def error_result(unit_id: int, code: ErrorCode, *, target: str = "$") -> UnitResult:
    """A unit that produced no verdict: fixed code, no engine text, one root-level finding."""
    return UnitResult(
        id=unit_id,
        outcome="error",
        findings=[FindingWire(outcome="error", pointer="", path=target, error_code=code)],
        error_code=code,
    )


class WorkerReady(_Wire):
    ready: Literal[True] = True
    engine_version: str
    dsl_version: str = DSL_VERSION


class StartMarker(_Wire):
    start: int


class DoneMarker(_Wire):
    done: int
    result: UnitResult


class EndMarker(_Wire):
    end: Literal[True] = True
