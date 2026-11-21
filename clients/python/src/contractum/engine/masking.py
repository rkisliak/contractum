"""Sample masking: which columns hide their values, and how a value is shown.

A failure sample is the only place a dataset value can reach a report, a log line or a terminal.
``should_mask`` decides from the contract's classification (or the ``--mask`` mode) and
``render_value`` produces the text a sample carries: a masked value shows only its Arrow type and
length (never a hash), any other value is cut to ``MAX_VALUE_CHARS`` and has control and
bidirectional characters escaped so it cannot rewrite a terminal or reorder text.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

MaskMode = Literal["auto", "all", "none"]

MAX_VALUE_CHARS = 80

_SENSITIVE = re.compile(r"^(pii|restricted|confidential|sensitive|secret|[345])$", re.IGNORECASE)
# Format characters (bidi controls, zero-width joiners), line and paragraph separators, controls,
# surrogates and private use are all shown as escapes.
_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


def is_sensitive(classification: str | None) -> bool:
    """Whether a property classification names restricted data."""
    return classification is not None and _SENSITIVE.match(classification.strip()) is not None


def should_mask(classification: str | None, mode: MaskMode) -> bool:
    """Whether samples of a column with this classification are masked under ``mode``."""
    if mode == "all":
        return True
    return mode == "auto" and is_sensitive(classification)


def _escape(char: str) -> str:
    code = ord(char)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def _shown(char: str) -> str:
    return _escape(char) if unicodedata.category(char) in _ESCAPED_CATEGORIES else char


def render_value(value: Any, arrow_type: Any, *, masked: bool) -> str:
    """The text a failure sample shows for a value of the given Arrow type.

    Masked values become ``<masked TYPE len=N>``. Others are shown with control and bidirectional
    characters escaped and cut after 80 characters (``...`` marks a cut).
    """
    if value is None:
        return "null"
    text = str(value)
    if masked:
        return f"<masked {arrow_type} len={len(text)}>"
    parts: list[str] = []
    size = 0
    for char in text:
        shown = _shown(char)
        if size + len(shown) > MAX_VALUE_CHARS:
            parts.append("...")
            break
        parts.append(shown)
        size += len(shown)
    return "".join(parts)
