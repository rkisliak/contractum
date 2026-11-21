"""Evaluate one rule (JSONPath target + CEL condition) against one document.

The caller normalises the document once (`dc_rules.normalize.normalize_document`) and compiles
the condition and target. Outcomes are pass, fail or error; an evaluation error is never a pass
or a fail, and an error on one node never stops evaluation of the remaining nodes (D-139).
A finding carries only an error code, never the engine's text (it can echo document values).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from dc_rules.model import RuleDef
from dc_rules.ports import (
    ErrorCode,
    ExpressionEngine,
    Limits,
    Location,
    RuleFinding,
    RuleResult,
    Targeter,
    TooManyMatches,
)
from dc_rules.targets import TargetError

# CEL string literals (optionally raw), double or single quoted. Triple-quoted literals are
# matched as an empty literal followed by a quoted run, which is enough for an identifier scan.
_STRING_LITERAL = re.compile(r"""[rR]?"(?:[^"\\\n]|\\.)*"|[rR]?'(?:[^'\\\n]|\\.)*'""")
_CONTRACT_WORD = re.compile(r"(?<![A-Za-z0-9_.])contract(?![A-Za-z0-9_])")
_DOCUMENT_WORD = re.compile(r"(?<![A-Za-z0-9_.])(?:contract|previous)(?![A-Za-z0-9_])")


def references_contract(condition: str) -> bool:
    """True when the bare identifier ``contract`` occurs outside string literals.

    Conservative on purpose: a field access such as ``this.contract`` does not count, a
    reference such as ``has(contract.x)`` does.
    """
    return _CONTRACT_WORD.search(_STRING_LITERAL.sub('""', condition)) is not None


def references_document_binding(condition: str) -> bool:
    """True when ``contract`` or ``previous`` (a document-sized binding) occurs outside strings.

    ``changes`` and ``refs`` are small and bounded, so they do not select the lower match cap.
    """
    return _DOCUMENT_WORD.search(_STRING_LITERAL.sub('""', condition)) is not None


def _finding(
    outcome: Literal["fail", "error"],
    pointer: str,
    path: str,
    error_code: ErrorCode | None = None,
) -> RuleFinding:
    return RuleFinding(outcome, Location(pointer, path), error_code)


def _result(rule: RuleDef, findings: list[RuleFinding], matched: int) -> RuleResult:
    if any(f.outcome == "error" for f in findings):
        outcome: Literal["pass", "fail", "error"] = "error"
    elif findings:
        outcome = "fail"
    else:
        outcome = "pass"
    return RuleResult(rule.key, outcome, tuple(findings), matched)


def evaluate_rule(
    rule: RuleDef,
    condition: Any,
    target: Any,
    document: Any,
    ctx: Mapping[str, Any],
    *,
    engine: ExpressionEngine,
    targeter: Targeter,
    limits: Limits,
    extra_bindings: Mapping[str, Any] | None = None,
) -> RuleResult:
    """``extra_bindings`` are the per-job cel-2 bindings (``previous``, ``changes``, ``refs``)."""
    cap = (
        limits.max_matches_with_contract
        if references_document_binding(rule.condition)
        else limits.max_matches
    )
    shared = {"contract": document, "ctx": ctx, **(extra_bindings or {})}
    root_path = rule.target
    matched = 0
    findings: list[RuleFinding] = []
    first_error: ErrorCode | None = None

    try:
        for match in targeter.select(target, document, cap):
            matched += 1
            verdict = engine.evaluate(condition, {"this": match.node, **shared})
            if verdict is True:
                if rule.match == "any":
                    return _result(rule, [], matched)
                continue
            if verdict is False:
                if rule.match == "each":
                    findings.append(_finding("fail", match.pointer, match.path))
                continue
            first_error = first_error or verdict
            if rule.match == "each":
                findings.append(_finding("error", match.pointer, match.path, verdict))
    except TooManyMatches:
        return _result(rule, [_finding("error", "", root_path, "too_many_matches")], matched)
    except TargetError:
        return _result(rule, [_finding("error", "", root_path, "target_error")], matched)

    if matched == 0:
        if rule.on_empty == "fail":
            return _result(rule, [_finding("fail", "", root_path)], 0)
        return _result(rule, [], 0)

    if rule.match == "any":
        if first_error is not None:
            return _result(rule, [_finding("error", "", root_path, first_error)], matched)
        return _result(rule, [_finding("fail", "", root_path)], matched)
    return _result(rule, findings, matched)
