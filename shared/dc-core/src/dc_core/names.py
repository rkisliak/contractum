"""The one rule for user-entered names (quick 261120-lbz, D-01).

A name is what other people read in "by whom" cells, titles and audit rows, so it must not be able
to spoof them. Allowed: Unicode letters (L*), marks (M*), numbers (N*), the regular space U+0020 and
the punctuation ``- _ . , ( ) & ' / : # + @``. Refused: emoji and pictographs, symbols (So, Sk, Sm
except ``+``, Sc), controls (Cc), format characters (Cf: bidi embedding, override and isolate
points, zero-width characters, BOM), private use, unassigned and surrogate code points, line and
paragraph separators, tabs, newlines and every space other than U+0020. Variation selectors and
combining enclosing marks (category M, but they turn allowed characters into emoji) are refused
as well.

The value is checked as given first (so a tab or newline anywhere, ends included, is refused),
then U+0020 is stripped at both ends, then an empty result is refused. The problem names the first
offending character as ``U+XXXX`` and never echoes it. Only new writes are checked: stored names
are read as they are. :data:`NameRule` is the Pydantic marker; :func:`check_name` is for callers
that validate by hand. The UI twin is ``ui/src/components/form/nameRule.ts``.
"""

from __future__ import annotations

import unicodedata

from pydantic import AfterValidator
from pydantic_core import PydanticCustomError

NAME_PUNCTUATION = frozenset(" -_.,()&'/:#+@")
NAME_RULE_HINT = "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only."
BLANK_DETAIL = "Must not be blank."

_ALLOWED_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Mc", "Me", "Nd", "Nl", "No"})
# Marks that make an allowed character an emoji or pictograph: variation selectors (U+FE00 to
# U+FE0F, U+E0100 to U+E01EF) and the combining enclosing marks (U+20DD to U+20E4, the keycap).
_REFUSED_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF), (0x20DD, 0x20E4))


class NameRuleError(ValueError):
    """A name broke the rule; ``detail`` is the fixed, input-free explanation."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def code_point(char: str) -> str:
    """``U+`` plus at least four upper-case hex digits for one character."""
    return f"U+{ord(char):04X}"


def _allowed(char: str) -> bool:
    if char in NAME_PUNCTUATION:
        return True
    code = ord(char)
    if any(low <= code <= high for low, high in _REFUSED_RANGES):
        return False
    return unicodedata.category(char) in _ALLOWED_CATEGORIES


def first_disallowed(value: str) -> str | None:
    """The first character of ``value`` the rule refuses, or None."""
    for char in value:
        if not _allowed(char):
            return char
    return None


def disallowed_detail(value: str) -> str | None:
    """The refusal text for the first offending character of ``value``, or None (blank is fine)."""
    bad = first_disallowed(value)
    if bad is None:
        return None
    return f"Contains {code_point(bad)}, which is not allowed. {NAME_RULE_HINT}"


def name_problem(value: str) -> tuple[str | None, str]:
    """``(detail, trimmed)``: the refusal text (None when fine) and the trimmed value."""
    bad_detail = disallowed_detail(value)
    if bad_detail is not None:
        return bad_detail, value
    trimmed = value.strip(" ")
    if trimmed == "":
        return BLANK_DETAIL, trimmed
    return None, trimmed


def check_name(value: str) -> str:
    """The trimmed name, or :class:`NameRuleError`."""
    detail, trimmed = name_problem(value)
    if detail is not None:
        raise NameRuleError(detail)
    return trimmed


def _validate(value: str) -> str:
    detail, trimmed = name_problem(value)
    if detail is not None:
        # The detail has no braces and no input, so it is its own message template.
        raise PydanticCustomError("name_invalid", detail)
    return trimmed


NameRule = AfterValidator(_validate)
"""Pydantic marker: ``Annotated[str, StringConstraints(min_length=1, max_length=N), NameRule]``."""

__all__ = [
    "BLANK_DETAIL",
    "NAME_PUNCTUATION",
    "NAME_RULE_HINT",
    "NameRule",
    "NameRuleError",
    "check_name",
    "code_point",
    "disallowed_detail",
    "first_disallowed",
    "name_problem",
]
