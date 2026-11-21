"""Document normalisation before binding into the CEL engine.

The engine converts the bound document lazily and rejects any integer outside the signed and
unsigned 64-bit range, which turns *every* evaluation touching that subtree into an error. The
ODCS ingest gate accepts arbitrary YAML integers, so the caller normalises once per document:
out-of-range integers become floats. The walk is iterative (a hostile document can nest deeply)
and returns a new structure; the input is never mutated.
"""

from __future__ import annotations

from typing import Any

_INT_MIN = -(2**63)
_UINT_MAX = 2**64 - 1


def _scalar(value: Any) -> Any:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and not _INT_MIN <= value <= _UINT_MAX
    ):
        return float(value)
    return value


def _empty_like(value: Any) -> Any:
    return [] if isinstance(value, list) else {}


def _is_container(value: Any) -> bool:
    return isinstance(value, list | dict)


def normalize_document(document: Any) -> Any:
    """Return a copy of ``document`` with out-of-range integers replaced by floats."""
    if not _is_container(document):
        return _scalar(document)
    root = _empty_like(document)
    stack: list[tuple[Any, Any]] = [(document, root)]
    while stack:
        source, target = stack.pop()
        items = enumerate(source) if isinstance(source, list) else source.items()
        for key, value in items:
            if _is_container(value):
                child = _empty_like(value)
                stack.append((value, child))
            else:
                child = _scalar(value)
            if isinstance(target, list):
                target.append(child)
            else:
                target[key] = child
    return root
