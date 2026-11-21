"""The evaluation contract: limits, outcomes, result types and the two engine ports.

`evaluate_rule` depends only on `ExpressionEngine` and `Targeter`, never on a concrete engine,
so the CEL runtime can be swapped (a conformance suite pins each adapter).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class Limits:
    """Authoring and evaluation limits in one place.

    Field names equal the validation service's setting names.
    """

    max_expression_bytes: int = 2048
    max_target_chars: int = 512
    max_rules_per_set: int = 200
    max_compiled_bytes: int = 32768
    max_matches: int = 10_000
    max_matches_with_contract: int = 100
    max_fixtures_per_kind: int = 5
    max_fixture_bytes: int = 65536


Outcome = Literal["pass", "fail", "error"]

ErrorCode = Literal[
    "no_such_key",
    "type_error",
    "invalid_argument",
    "budget_exceeded",
    "memory_exceeded",
    "timeout",
    "not_boolean",
    "too_many_matches",
    "target_error",
    "run_budget_exceeded",
    "engine_error",
    "unresolvable_rule_set",
]

# Fixed English text per code. Engine text can echo document values and never reaches a result.
ERROR_MESSAGES: Mapping[ErrorCode, str] = {
    "no_such_key": "The condition referenced a field that does not exist.",
    "type_error": "The condition applied an operator or function to values of the wrong type.",
    "invalid_argument": "The condition raised an invalid argument error.",
    "budget_exceeded": "The condition exceeded the evaluation budget.",
    "memory_exceeded": "The condition exceeded the memory limit.",
    "timeout": "The rule did not finish within its time limit.",
    "not_boolean": "The condition did not produce true or false.",
    "too_many_matches": "The target matched more nodes than the limit allows.",
    "target_error": "The target could not be evaluated against the document.",
    "run_budget_exceeded": "The validation run exceeded its time budget before this rule ran.",
    "engine_error": "The rule engine reported an unexpected error.",
    "unresolvable_rule_set": "The rule set could not be resolved.",
}


@dataclass(frozen=True)
class Location:
    pointer: str  # RFC 6901 JSON Pointer; "" is the document root
    path: str  # normalised JSONPath


@dataclass(frozen=True)
class RuleFinding:
    outcome: Literal["fail", "error"]
    location: Location
    error_code: ErrorCode | None


@dataclass(frozen=True)
class RuleResult:
    rule_key: str
    outcome: Outcome
    findings: tuple[RuleFinding, ...]
    matched: int


@dataclass(frozen=True)
class Match:
    node: Any
    pointer: str
    path: str


class RuleCompileError(Exception):
    """A rule failed to compile. ``detail`` is author feedback only, never stored in a report."""

    def __init__(
        self, code: Literal["compile_error", "not_boolean", "target_invalid"], detail: str
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class TooManyMatches(Exception):
    """The target produced more matches than the cap."""


class ExpressionEngine(Protocol):
    name: str
    version: str

    def compile(self, expression: str, dsl_version: str = "cel-1") -> Any: ...

    def evaluate(self, compiled: Any, bindings: Mapping[str, Any]) -> bool | ErrorCode: ...

    def compiled_size(self, compiled: Any) -> int: ...


class Targeter(Protocol):
    def compile(self, path: str) -> Any: ...

    def select(self, compiled: Any, document: Any, cap: int) -> Iterator[Match]: ...
