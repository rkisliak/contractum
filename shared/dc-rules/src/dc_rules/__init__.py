"""dc-rules: declarative rule engine core.

This package root re-exports only the pure names (model and ports). It never imports
``cel_engine``, ``targets`` or ``evaluator``, so a process can use the rule model without loading
the C++ CEL engine.
"""

from dc_rules.model import (
    DSL_VERSION,
    Fixture,
    FixtureCtx,
    MatchMode,
    OnEmpty,
    RuleDef,
    RuleFixtures,
    RuleSetContent,
    Severity,
    canonical_json,
    content_sha256,
)
from dc_rules.ports import (
    ERROR_MESSAGES,
    ErrorCode,
    ExpressionEngine,
    Limits,
    Location,
    Match,
    Outcome,
    RuleCompileError,
    RuleFinding,
    RuleResult,
    Targeter,
    TooManyMatches,
)

__all__ = [
    "DSL_VERSION",
    "ERROR_MESSAGES",
    "ErrorCode",
    "ExpressionEngine",
    "Fixture",
    "FixtureCtx",
    "Limits",
    "Location",
    "Match",
    "MatchMode",
    "OnEmpty",
    "Outcome",
    "RuleCompileError",
    "RuleDef",
    "RuleFinding",
    "RuleFixtures",
    "RuleResult",
    "RuleSetContent",
    "Severity",
    "Targeter",
    "TooManyMatches",
    "canonical_json",
    "content_sha256",
]
