"""Keyed collections and the tiered pairing of their items (D-144).

Items are paired between the two documents, never keyed independently per side: a property that
gains an ``id`` must still be the same property, and ordering is never content. Pairing runs in
tiers with dict buckets (O(n), never nested loops):

0. equal ``id`` (a different natural key on top of an equal id is a rename),
1. equal natural key (different ids on both sides are an ``id-changed``),
2. equal canonical content (items with no natural key, and quality rules),
3. equal "what it checks" fields (quality rules only).

Leftover items on the first side are removed, on the second side added. Items sharing a natural
key get the suffixes ``name``, ``name~2``, ``name~3`` in document order, so duplicates never crash
the diff.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

CHECK_FIELDS = ("type", "metric", "rule", "query", "engine", "implementation")


class _Lit(str):
    """A literal token on the canonicaliser stack (kept apart from document strings)."""


def canon(value: Any) -> str:
    """A canonical string of any JSON-like value (sorted keys, ``10`` equals ``10.0``).

    Iterative, so an arbitrarily deep value cannot exhaust the interpreter stack.
    """
    out: list[str] = []
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, _Lit):
            out.append(item)
        elif isinstance(item, dict):
            keys = sorted(item, key=str)
            stack.append(_Lit("}"))
            for position in range(len(keys) - 1, -1, -1):
                stack.append(item[keys[position]])
                stack.append(_Lit(json.dumps(str(keys[position])) + ":"))
                if position:
                    stack.append(_Lit(","))
            stack.append(_Lit("{"))
        elif isinstance(item, list):
            stack.append(_Lit("]"))
            for position in range(len(item) - 1, -1, -1):
                stack.append(item[position])
                if position:
                    stack.append(_Lit(","))
            stack.append(_Lit("["))
        elif item is None:
            out.append("null")
        elif isinstance(item, bool):
            out.append("true" if item else "false")
        elif isinstance(item, float) and item.is_integer():
            out.append(str(int(item)))
        elif isinstance(item, int | float):
            out.append(repr(item))
        elif isinstance(item, str):
            out.append(json.dumps(item))
        else:
            out.append(json.dumps(str(item)))
    return "".join(out)


def same(left: Any, right: Any) -> bool:
    """Equality that keeps ``true`` apart from ``1`` and treats ``10`` and ``10.0`` as equal."""
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return canon(left) == canon(right)


@dataclass(frozen=True)
class CollectionSpec:
    """How one keyed collection is paired.

    ``key_fields``: the natural key (several fields are joined as ``a@b``, the first is
    required). ``relationship``: key on the canonical ``from`` and ``to``. ``rename``: an equal
    id with another natural key is a rename. ``content_tiers``: quality-style content and check
    tiers. ``whole``: introducing or dropping the whole collection is one change (enum).
    """

    child_kind: str
    key_fields: tuple[str, ...] = ()
    relationship: bool = False
    rename: bool = False
    content_tiers: bool = False
    whole: bool = False


_ENTITY_PARENTS = ("object", "property")

KEYED_COLLECTIONS: dict[tuple[str, str], CollectionSpec] = {
    ("contract", "schema"): CollectionSpec("object", ("name",), rename=True),
    ("object", "properties"): CollectionSpec("property", ("name",), rename=True),
    ("property", "properties"): CollectionSpec("property", ("name",), rename=True),
    ("property", "enum"): CollectionSpec("enum-value", ("value",), whole=True),
    ("contract", "servers"): CollectionSpec("server", ("server",)),
    ("server", "roles"): CollectionSpec("server-role", ("role",)),
    ("contract", "slaProperties"): CollectionSpec("sla", ("property", "element")),
    ("contract", "roles"): CollectionSpec("role", ("role",)),
    ("contract", "support"): CollectionSpec("support", ("channel",)),
    ("team", "members"): CollectionSpec("team-member", ("username",)),
    ("context", "verifiedStatements"): CollectionSpec("context-statement", ("question",)),
    ("context", "constraints"): CollectionSpec("context-constraint", ("constraint",)),
    ("*", "customProperties"): CollectionSpec("custom-property", ("property",)),
    ("*", "authoritativeDefinitions"): CollectionSpec("authoritative-definition", ("url",)),
}
for _parent in _ENTITY_PARENTS:
    KEYED_COLLECTIONS[(_parent, "relationships")] = CollectionSpec(
        "relationship", relationship=True
    )
    KEYED_COLLECTIONS[(_parent, "quality")] = CollectionSpec(
        "quality", ("name",), content_tiers=True
    )
    KEYED_COLLECTIONS[(_parent, "synonyms")] = CollectionSpec("synonym", ("synonym",))

# Scalar sets: order is never a change (``logicalTypeOptions.required`` is handled by the walk).
SCALAR_SETS = frozenset({"tags", "examples", "transformSourceObjects"})

# Single-object children compared in place: ``(parent kind, key) -> child kind``.
SINGLE_CHILDREN: dict[tuple[str, str], str] = {
    ("property", "items"): "property",
    ("property", "map"): "map",
    ("map", "key"): "property",
    ("map", "value"): "property",
    ("contract", "team"): "team",
    ("*", "context"): "context",
}


def spec_for(kind: str, key: str) -> CollectionSpec | None:
    return KEYED_COLLECTIONS.get((kind, key)) or KEYED_COLLECTIONS.get(("*", key))


def single_child_kind(kind: str, key: str) -> str | None:
    return SINGLE_CHILDREN.get((kind, key)) or SINGLE_CHILDREN.get(("*", key))


@dataclass(frozen=True)
class Item:
    index: int
    value: Any
    ident: str | None
    natural: str | None
    key: str


@dataclass(frozen=True)
class Pair:
    """One pairing outcome: ``before``/``after`` is ``None`` for an added or removed item."""

    before: Item | None
    after: Item | None
    tier: int

    @property
    def key(self) -> str:
        item = self.after if self.after is not None else self.before
        return item.key if item is not None else ""


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    return canon(value)


def _relation_side(value: Any) -> str:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    return _text(value) or ""


def _natural(spec: CollectionSpec, value: Any) -> str | None:
    if not isinstance(value, dict):
        return _text(value)
    if spec.relationship:
        target = _relation_side(value.get("to"))
        if not target:
            return None
        return f"{_relation_side(value.get('from'))}>{target}"
    parts = [_text(value.get(name)) for name in spec.key_fields]
    if parts[0] is None:
        return None
    return "@".join(part for part in parts if part is not None)


def _prepare(spec: CollectionSpec, items: list[Any]) -> list[Item]:
    seen: dict[str, int] = {}
    out: list[Item] = []
    for index, value in enumerate(items):
        ident = _text(value.get("id")) if isinstance(value, dict) else None
        natural = _natural(spec, value)
        if natural is not None:
            base = natural
        elif ident is not None:
            base = f"id={ident}"
        else:
            base = "#" + hashlib.sha256(canon(value).encode()).hexdigest()[:8]
        count = seen.get(base, 0) + 1
        seen[base] = count
        out.append(Item(index, value, ident, natural, base if count == 1 else f"{base}~{count}"))
    return out


def _check_signature(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    fields = [value.get(name) for name in CHECK_FIELDS]
    if all(field is None for field in fields):
        return None
    return canon(fields)


def pair_items(spec: CollectionSpec, before: list[Any], after: list[Any]) -> list[Pair]:
    """Pair the items of one collection; the result is ordered by key for a stable walk."""
    left = _prepare(spec, before)
    right = _prepare(spec, after)
    matched: dict[int, tuple[Item, int]] = {}
    taken: set[int] = set()

    def run(
        tier: int,
        wants: Any,
        bucket_key: Any,
    ) -> None:
        buckets: dict[str, deque[Item]] = {}
        for item in right:
            if item.index not in taken and wants(item):
                label = bucket_key(item)
                if label is not None:
                    buckets.setdefault(label, deque()).append(item)
        for item in left:
            if item.index in matched or not wants(item):
                continue
            label = bucket_key(item)
            if label is not None and buckets.get(label):
                match = buckets[label].popleft()
                matched[item.index] = (match, tier)
                taken.add(match.index)

    run(0, lambda item: True, lambda item: item.ident)
    run(1, lambda item: True, lambda item: item.key if item.natural is not None else None)
    run(
        2,
        lambda item: spec.content_tiers or item.natural is None,
        lambda item: canon(item.value),
    )
    if spec.content_tiers:
        run(3, lambda item: True, lambda item: _check_signature(item.value))

    pairs: list[Pair] = []
    for item in left:
        if item.index in matched:
            match, tier = matched[item.index]
            pairs.append(Pair(item, match, tier))
        else:
            pairs.append(Pair(item, None, -1))
    pairs.extend(Pair(None, item, -1) for item in right if item.index not in taken)
    pairs.sort(key=lambda pair: (pair.key, pair.before.index if pair.before else -1))
    return pairs


__all__ = [
    "CHECK_FIELDS",
    "KEYED_COLLECTIONS",
    "SCALAR_SETS",
    "SINGLE_CHILDREN",
    "CollectionSpec",
    "Item",
    "Pair",
    "canon",
    "pair_items",
    "same",
    "single_child_kind",
    "spec_for",
]
