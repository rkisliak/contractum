"""Constraint bounds as intervals: tightened, widened or both (D-145).

The two sides of a ``logicalTypeOptions`` are compared as accepted intervals, so ``maximum: 10``
becoming ``exclusiveMaximum: 10`` is a tightening and the reverse a widening. Numbers are compared
exactly (no rounding; ``10`` equals ``10.0``). A bound that is not a number (an ISO date string,
say) is opaque: equal text is equal, anything else is conservatively ``both``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from dc_odcs.diff.normalise import canon

Verdict = Literal["equal", "tightened", "widened", "both"]


@dataclass(frozen=True)
class _Group:
    lower: bool
    keywords: tuple[str, ...]


GROUPS: Final = (
    _Group(True, ("minLength",)),
    _Group(False, ("maxLength",)),
    _Group(True, ("minimum", "exclusiveMinimum")),
    _Group(False, ("maximum", "exclusiveMaximum")),
    _Group(True, ("minItems",)),
    _Group(False, ("maxItems",)),
    _Group(True, ("minProperties",)),
    _Group(False, ("maxProperties",)),
)
BOUND_KEYWORDS: Final = frozenset(keyword for group in GROUPS for keyword in group.keywords)


@dataclass(frozen=True)
class Bound:
    """The effective bound of one side of one group."""

    keyword: str
    value: Any
    exclusive: bool
    numeric: bool
    opaque: str


@dataclass(frozen=True)
class GroupResult:
    """One group whose bound differs between the two documents."""

    attribute: str
    verdict: Verdict
    before: Bound | None
    after: Bound | None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _effective(group: _Group, options: dict[str, Any]) -> Bound | None:
    present = [(name, options[name]) for name in group.keywords if options.get(name) is not None]
    if not present:
        return None
    if not all(_is_number(value) for _, value in present):
        name, value = present[0]
        return Bound(name, value, False, False, canon(present))

    def rank(entry: tuple[str, Any]) -> tuple[Any, int]:
        name, value = entry
        exclusive = name.startswith("exclusive")
        # tighter sorts last for a lower bound and first for an upper bound
        return (value, 1 if exclusive else 0) if group.lower else (value, 0 if exclusive else 1)

    name, value = max(present, key=rank) if group.lower else min(present, key=rank)
    return Bound(name, value, name.startswith("exclusive"), True, "")


def _side(group: _Group, before: Bound | None, after: Bound | None) -> Verdict:
    if before is None and after is None:
        return "equal"
    if before is None:
        return "tightened"
    if after is None:
        return "widened"
    if not (before.numeric and after.numeric):
        return "equal" if before.opaque == after.opaque else "both"
    if before.value == after.value and before.exclusive == after.exclusive:
        return "equal"
    if before.value == after.value:
        after_tighter = after.exclusive
    elif group.lower:
        after_tighter = after.value > before.value
    else:
        after_tighter = after.value < before.value
    return "tightened" if after_tighter else "widened"


def compare_groups(before: dict[str, Any], after: dict[str, Any]) -> list[GroupResult]:
    """The bound groups that differ, each with its direction and effective bounds."""
    results: list[GroupResult] = []
    for group in GROUPS:
        left = _effective(group, before)
        right = _effective(group, after)
        verdict = _side(group, left, right)
        current = right if right is not None else left
        if verdict != "equal" and current is not None:
            results.append(GroupResult(current.keyword, verdict, left, right))
    return results


def compare_bounds(before: dict[str, Any], after: dict[str, Any]) -> Verdict:
    """The overall direction of the interval change between two ``logicalTypeOptions``."""
    seen = {result.verdict for result in compare_groups(before, after)}
    if not seen:
        return "equal"
    if "both" in seen or len(seen) == 2:
        return "both"
    return "tightened" if "tightened" in seen else "widened"


__all__ = [
    "BOUND_KEYWORDS",
    "GROUPS",
    "Bound",
    "GroupResult",
    "Verdict",
    "compare_bounds",
    "compare_groups",
]
