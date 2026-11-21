"""JSON pointer (RFC 6901) string lookup over a parsed document."""

from __future__ import annotations

from typing import Any


def _unescape(token: str) -> str:
    """Undo the pointer escapes in RFC order: ``~1`` first, then ``~0``."""
    return token.replace("~1", "/").replace("~0", "~")


def string_at(document: Any, pointer: str) -> str | None:
    """The string value at ``pointer`` of ``document``, or ``None``.

    ``None`` covers a missing key, an index that is not a decimal number or is out of range, a
    path that runs into a scalar, and a leaf that is not a string. It never raises.
    """
    node = document
    for token in pointer.split("/")[1:]:
        key = _unescape(token)
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and key.isascii() and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return None
    return node if isinstance(node, str) else None
