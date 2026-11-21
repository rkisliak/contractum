"""Text-input guards shared by identity's request models and query parameters.

PostgreSQL text values cannot contain NUL (U+0000): psycopg raises ``DataError`` when one is bound,
which would surface as a 500. Every free-text field and text filter that can reach a bound
parameter carries :data:`NoNul`, so the caller gets a 422 ``validation-error`` instead.
"""

from __future__ import annotations

import unicodedata
from typing import Annotated

from dc_core.names import NameRule
from pydantic import AfterValidator, StringConstraints


def reject_nul(value: str) -> str:
    """Return ``value`` unchanged, or raise ``ValueError`` (a 422) when it holds a NUL."""
    if "\x00" in value:
        raise ValueError("NUL (U+0000) characters are not allowed")
    return value


NoNul = AfterValidator(reject_nul)
"""Pydantic validator marker: ``Annotated[str, NoNul]`` refuses NUL characters."""

NoNulStr = Annotated[str, NoNul]

# Bidirectional embedding, override and isolate code points: they can visually reorder a name and
# spoof it in "by whom" cells. ``reject_control`` still serves free text that is not a name;
# names themselves follow the one rule of dc_core.names (quick 261120-lbz, D-01), which refuses
# every format character including the zero-width joiners (this supersedes the old allowance).
_BIDI_FORMATTING = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_REFUSED_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


def reject_control(value: str) -> str:
    """Return ``value`` unchanged, or raise ``ValueError`` for control or bidi characters.

    Refused: Unicode category Cc (C0, DEL and C1, which includes NUL, tab and newline), Zl and Zp
    (line and paragraph separators) and the bidirectional embedding, override and isolate points.
    """
    for char in value:
        if char in _BIDI_FORMATTING or unicodedata.category(char) in _REFUSED_CATEGORIES:
            raise ValueError("control or bidirectional formatting characters are not allowed")
    return value


DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=200), NameRule]
"""A display name: 1-200 characters under the one name rule of :mod:`dc_core.names` (regular
spaces trimmed; NUL is refused earlier by the route's NUL guard)."""

__all__ = ["DisplayName", "NoNul", "NoNulStr", "reject_control", "reject_nul"]
