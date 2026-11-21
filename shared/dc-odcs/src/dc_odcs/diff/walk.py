"""The lockstep walk over two documents: pair, compare and emit classified changes.

The walk is iterative (an explicit stack, never recursion, so a deeply nested document cannot
exhaust the interpreter stack). Paths are built from natural keys (``schema.orders.properties.id``,
a segment that contains a dot is bracket-quoted); pointers keep the original array indexes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from dc_odcs.diff import table
from dc_odcs.diff.bounds import BOUND_KEYWORDS, compare_groups
from dc_odcs.diff.model import Change, capped_value, value_allowed
from dc_odcs.diff.normalise import (
    SCALAR_SETS,
    CollectionSpec,
    canon,
    pair_items,
    same,
    single_child_kind,
    spec_for,
)

_MISSING: Final = object()
_PLAIN_SEGMENT: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-@~#:=/ >"
)
_ENTITIES: Final = ("object", "property")

# Never compared as content (the table owns the list).
_SKIPPED: Final = table.EXCLUDED


def escape(token: str) -> str:
    """JSON Pointer token escaping (same as ``dc_odcs.refs``)."""
    return token.replace("~", "~0").replace("/", "~1")


def join_path(path: str, segment: str) -> str:
    """Append a segment to a name-keyed path; a segment with a dot or bracket is quoted."""
    if segment and all(char in _PLAIN_SEGMENT for char in segment):
        return f"{path}.{segment}" if path else segment
    return f"{path}[{json.dumps(segment)}]"


@dataclass(frozen=True)
class _Side:
    value: Any
    ptr: str
    # the deprecated array form of ``team`` has its members directly under the team pointer
    flat: bool = False

    def child(self, key: str) -> str:
        return self.ptr if self.flat and key == "members" else f"{self.ptr}/{escape(key)}"


@dataclass(frozen=True)
class _Frame:
    kind: str
    path: str
    before: _Side
    after: _Side


class _Sink:
    def __init__(self, max_changes: int) -> None:
        self.max_changes = max_changes
        self.changes: list[Change] = []
        self.counts: dict[str, int] = {"breaking": 0, "warning": 0, "info": 0}
        self.total = 0

    def add(self, change: Change) -> None:
        self.total += 1
        self.counts[change.cls] += 1
        # past the cap changes are counted but not stored (T-6-01)
        if len(self.changes) < self.max_changes:
            self.changes.append(change)

    def attribute(
        self,
        frame: _Frame,
        attribute: str,
        direction: str,
        before: Any,
        after: Any,
        ptr_before: str | None,
        ptr_after: str | None,
    ) -> None:
        rule = table.classify_attribute(frame.kind, attribute, direction, before, after)
        keep = value_allowed(frame.kind, attribute)
        self.add(
            Change(
                rule.code,
                rule.cls,
                frame.kind,
                frame.path,
                ptr_before,
                ptr_after,
                attribute,
                capped_value(before) if keep else None,
                capped_value(after) if keep else None,
            )
        )

    def element(
        self,
        kind: str,
        path: str,
        action: str,
        node: Any,
        ptr_before: str | None,
        ptr_after: str | None,
    ) -> None:
        rule = table.classify_element(kind, action, node)
        attribute = None
        before = after = None
        if kind == "enum-value" and isinstance(node, dict):
            attribute = "enum"
            value = capped_value(node.get("value"))
            before, after = (value, None) if action == "removed" else (None, value)
        self.add(
            Change(rule.code, rule.cls, kind, path, ptr_before, ptr_after, attribute, before, after)
        )


def _absent(value: Any) -> bool:
    return value is _MISSING or value is None


def _is_list(value: Any) -> bool:
    return isinstance(value, list) or _absent(value)


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _leaf(
    frame: _Frame,
    sink: _Sink,
    key: str,
    before: Any,
    after: Any,
    base_before: str,
    base_after: str,
) -> None:
    if key in table.BOOLEAN_ATTRIBUTES:
        was, now = before is True, after is True
        if was == now:
            return
        direction = "to-true" if now else "to-false"
    else:
        if _absent(before) and _absent(after):
            return
        if _absent(before):
            direction = "added"
        elif _absent(after):
            direction = "removed"
        elif same(before, after):
            return
        else:
            direction = "changed"
    sink.attribute(
        frame,
        key,
        direction,
        None if before is _MISSING else before,
        None if after is _MISSING else after,
        None if _absent(before) else f"{base_before}/{escape(key)}",
        None if _absent(after) else f"{base_after}/{escape(key)}",
    )


def _scalar_set(
    frame: _Frame,
    sink: _Sink,
    attribute: str,
    before: Any,
    after: Any,
    ptr_before: str,
    ptr_after: str,
) -> None:
    left = {canon(item) for item in _items(before)}
    right = {canon(item) for item in _items(after)}
    if right - left:
        sink.attribute(frame, attribute, "added", None, None, None, ptr_after)
    if left - right:
        sink.attribute(frame, attribute, "removed", None, None, ptr_before, None)


def _walk_options(frame: _Frame, sink: _Sink, before: Any, after: Any) -> None:
    """``logicalTypeOptions``: bounds as intervals, ``required`` as a set, the rest as leaves."""
    left = before if isinstance(before, dict) else {}
    right = after if isinstance(after, dict) else {}
    base_b = f"{frame.before.ptr}/logicalTypeOptions"
    base_a = f"{frame.after.ptr}/logicalTypeOptions"
    for result in compare_groups(left, right):
        rule = table.classify_bound(result.verdict)
        gone, new = result.before, result.after
        sink.add(
            Change(
                rule.code,
                rule.cls,
                frame.kind,
                frame.path,
                f"{base_b}/{gone.keyword}" if gone is not None else None,
                f"{base_a}/{new.keyword}" if new is not None else None,
                result.attribute,
                capped_value(gone.value)
                if gone is not None and value_allowed(frame.kind, result.attribute)
                else None,
                capped_value(new.value)
                if new is not None and value_allowed(frame.kind, result.attribute)
                else None,
            )
        )
    for key in sorted(set(left) | set(right), key=str):
        if key in BOUND_KEYWORDS:
            continue
        if key == "required" and _is_list(left.get(key)) and _is_list(right.get(key)):
            _scalar_set(
                frame,
                sink,
                "logicalTypeOptions.required",
                left.get(key),
                right.get(key),
                f"{base_b}/required",
                f"{base_a}/required",
            )
            continue
        _leaf(frame, sink, key, left.get(key, _MISSING), right.get(key, _MISSING), base_b, base_a)


def _single_value(kind: str, key: str, value: Any) -> tuple[Any, bool]:
    """``(node, flat)``: a dict, ``None`` when absent, or ``_MISSING`` for an unwalkable shape."""
    if value is _MISSING or value is None:
        return None, False
    if isinstance(value, dict):
        return value, False
    if isinstance(value, list) and kind == "contract" and key == "team":
        return {"members": value}, True  # the deprecated array-of-members form
    return _MISSING, False


def _walk_collection(
    frame: _Frame,
    sink: _Sink,
    spec: CollectionSpec,
    key: str,
    left: Any,
    right: Any,
    children: list[_Frame],
) -> None:
    base = join_path(frame.path, key)
    ptr_b = frame.before.child(key)
    ptr_a = frame.after.child(key)
    before_items, after_items = _items(left), _items(right)
    if spec.whole and bool(before_items) != bool(after_items):
        values_before = [i.get("value") if isinstance(i, dict) else i for i in before_items]
        values_after = [i.get("value") if isinstance(i, dict) else i for i in after_items]
        sink.attribute(
            frame,
            key,
            "added" if after_items else "removed",
            values_before or None,
            values_after or None,
            ptr_b if before_items else None,
            ptr_a if after_items else None,
        )
        return
    for pair in pair_items(spec, before_items, after_items):
        path = join_path(base, pair.key)
        old, new = pair.before, pair.after
        if new is None and old is not None:
            sink.element(spec.child_kind, path, "removed", old.value, f"{ptr_b}/{old.index}", None)
        elif old is None and new is not None:
            sink.element(spec.child_kind, path, "added", new.value, None, f"{ptr_a}/{new.index}")
        elif old is not None and new is not None:
            if not (isinstance(old.value, dict) and isinstance(new.value, dict)):
                continue
            child = _Frame(
                spec.child_kind,
                path,
                _Side(old.value, f"{ptr_b}/{old.index}"),
                _Side(new.value, f"{ptr_a}/{new.index}"),
            )
            if pair.tier == 0 and spec.rename and old.natural != new.natural:
                sink.attribute(
                    child, "name", "renamed", None, None, child.before.ptr, child.after.ptr
                )
            if pair.tier >= 1 and old.ident and new.ident and old.ident != new.ident:
                sink.attribute(
                    child,
                    "id",
                    "changed",
                    None,
                    None,
                    f"{child.before.ptr}/id",
                    f"{child.after.ptr}/id",
                )
            children.append(child)


def _walk_element(frame: _Frame, stack: list[_Frame], sink: _Sink) -> None:
    before, after = frame.before.value, frame.after.value
    children: list[_Frame] = []
    for key in sorted(set(before) | set(after), key=str):
        if (frame.kind, key) in _SKIPPED or (key == "id" and frame.kind != "contract"):
            continue
        left = before.get(key, _MISSING)
        right = after.get(key, _MISSING)
        if (
            key == "logicalTypeOptions"
            and frame.kind in _ENTITIES
            and all(isinstance(side, dict) or _absent(side) for side in (left, right))
        ):
            _walk_options(frame, sink, left, right)
            continue
        spec = spec_for(frame.kind, key)
        if spec is not None and _is_list(left) and _is_list(right):
            _walk_collection(frame, sink, spec, key, left, right, children)
            continue
        if key in SCALAR_SETS and _is_list(left) and _is_list(right):
            _scalar_set(
                frame, sink, key, left, right, frame.before.child(key), frame.after.child(key)
            )
            continue
        child_kind = single_child_kind(frame.kind, key)
        if child_kind is not None:
            old, flat_b = _single_value(frame.kind, key, left)
            new, flat_a = _single_value(frame.kind, key, right)
            if old is not _MISSING and new is not _MISSING:
                path = join_path(frame.path, key)
                ptr_b = frame.before.child(key)
                ptr_a = frame.after.child(key)
                if old is None and new is not None:
                    sink.element(child_kind, path, "added", new, None, ptr_a)
                elif new is None and old is not None:
                    sink.element(child_kind, path, "removed", old, ptr_b, None)
                elif old is not None and new is not None:
                    children.append(
                        _Frame(
                            child_kind, path, _Side(old, ptr_b, flat_b), _Side(new, ptr_a, flat_a)
                        )
                    )
                continue
        _leaf(frame, sink, key, left, right, frame.before.ptr, frame.after.ptr)
    stack.extend(reversed(children))


def walk(before: dict[str, Any], after: dict[str, Any], *, max_changes: int) -> _Sink:
    """Walk two documents; the first ``max_changes`` changes are stored, all are counted.

    Keys and paired items are visited in sorted order, so the stored subset is deterministic.
    """
    sink = _Sink(max_changes)
    stack = [_Frame("contract", "", _Side(before, ""), _Side(after, ""))]
    while stack:
        _walk_element(stack.pop(), stack, sink)
    return sink
