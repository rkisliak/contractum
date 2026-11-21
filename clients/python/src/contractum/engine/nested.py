"""Columns of a dataset as the checks see them, including values nested in structs and lists.

``NestedColumn`` pairs the values a check judges with the rows they belong to. A top-level column
is its own values; a struct field or a flattened list element carries ``present`` (is the parent
there) and ``rows`` (the top-level row of each element), so a failure sample can cite the row.

``property_paths`` walks the nested and item properties of a contract object into check paths
(``shipping.city`` for a struct field, ``tags[]`` for list items, ``lines[].sku`` for a field of
list items), ``NestedView`` resolves such a path on a table with ``pc.struct_field``-style
flattening and ``pc.list_flatten`` plus ``pc.list_parent_indices``, and ``expand_nested`` resolves
every path of an object at once (D-47). Nesting deeper than ``MAX_DEPTH`` levels is cut off.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.types as pat

from contractum.engine.masking import is_sensitive
from contractum.engine.types import is_list
from contractum.odcs import Property, SchemaObject

MAX_DEPTH = 10

# One step of a path: a field name, or None for "flatten the list here".
PathStep = str | None
Steps = tuple[PathStep, ...]


@dataclass(frozen=True)
class NestedColumn:
    """The values one check judges.

    ``values`` is an Arrow array of the elements. ``present`` is a boolean array saying whether the
    parent of each element exists (None: every element counts), ``rows`` maps each element to the
    row of the top-level table (None: element ``i`` is row ``i``) and ``source_type`` is the type
    as the dataset spelled it, when it differs from the type the kernels see.
    """

    values: Any
    present: Any = None
    rows: Any = None
    source_type: Any = None

    @property
    def dtype(self) -> Any:
        return self.source_type if self.source_type is not None else self.values.type

    @property
    def total(self) -> int:
        """How many elements count: all of them, or those whose parent is present."""
        if self.present is None:
            return len(self.values)
        return int(pc.sum(self.present).as_py() or 0)

    def top_row(self, position: int) -> int:
        """The top-level row of the element at ``position``."""
        return position if self.rows is None else int(self.rows[position].as_py())


@dataclass(frozen=True)
class PropertyPath:
    """A property of an object, where it sits and how a dataset reaches it.

    ``column`` is the name checks report (``shipping.city``, ``tags[]``), ``steps`` the dataset
    path from the top-level column, ``depth`` the nesting level (1 for a top-level property),
    ``in_list`` whether a list is flattened on the way, ``classification`` the classification that
    applies (a sensitive ancestor wins) and ``truncated`` marks a property beyond the depth limit,
    which is reported and never evaluated.
    """

    prop: Property
    column: str
    steps: Steps
    depth: int
    in_list: bool = False
    classification: str | None = None
    truncated: bool = False

    @property
    def is_items(self) -> bool:
        """Whether this is the element property of an array."""
        return bool(self.steps) and self.steps[-1] is None


def _children(path: PropertyPath) -> list[PropertyPath]:
    prop, depth = path.prop, path.depth + 1
    inherited = path.classification if is_sensitive(path.classification) else None
    found = [
        PropertyPath(
            child,
            f"{path.column}.{child.name}",
            (*path.steps, child.source_name),
            depth,
            path.in_list,
            inherited or child.classification,
        )
        for child in prop.nested
    ]
    if prop.items is not None:
        found.append(
            PropertyPath(
                prop.items,
                f"{path.column}[]",
                (*path.steps, None),
                depth,
                True,
                inherited or prop.items.classification,
            )
        )
    return found


def property_paths(obj: SchemaObject, *, max_depth: int = MAX_DEPTH) -> list[PropertyPath]:
    """Every property of the object, depth first in contract order, with its nested paths.

    A property deeper than ``max_depth`` levels appears once as a truncated path and its own
    children are not walked.
    """
    paths: list[PropertyPath] = []
    pending = [
        PropertyPath(prop, prop.name, (prop.source_name,), 1, False, prop.classification)
        for prop in reversed(obj.properties)
    ]
    while pending:
        path = pending.pop()
        if path.depth > max_depth:
            paths.append(replace(path, truncated=True))
            continue
        paths.append(path)
        pending.extend(reversed(_children(path)))
    return paths


@dataclass(frozen=True)
class Missing:
    """A path that cannot be resolved on the table, with the reason a check is skipped.

    ``absent`` is True when everything up to the last step is there and only the last field is
    not (the one case a ``present`` check fails instead of being skipped).
    """

    reason: str
    absent: bool = False


@dataclass(frozen=True)
class _State:
    values: Any
    present: Any = None
    rows: Any = None


def _field_index(dtype: Any, name: str, ignore_case: bool) -> int | None:
    wanted = name.lower() if ignore_case else name
    for index in range(dtype.num_fields):
        found = dtype.field(index).name
        if (found.lower() if ignore_case else found) == wanted:
            return index
    return None


def _enter_field(state: _State, name: str, *, ignore_case: bool, last: bool) -> _State | Missing:
    values = state.values
    if not pat.is_struct(values.type):
        return Missing(f"cannot read field {name!r}: the parent is not an object")
    index = _field_index(values.type, name, ignore_case)
    if index is None:
        return Missing(f"field {name!r} is missing from the object", absent=last)
    parent_present = pc.is_valid(values)
    present = parent_present if state.present is None else pc.and_(state.present, parent_present)
    return _State(values.flatten()[index], present, state.rows)


def _enter_items(state: _State) -> _State | Missing:
    values = state.values
    if not is_list(values.type):
        return Missing(f"cannot read items: the parent is {values.type}, not an array")
    if pat.is_fixed_size_list(values.type):
        values = values.cast(pa.list_(values.type.value_type))
    parents = pc.list_parent_indices(values)
    rows = parents if state.rows is None else state.rows.take(parents)
    present = None if state.present is None else state.present.take(parents)
    return _State(pc.list_flatten(values), present, rows)


def _combined(column: Any) -> Any:
    return column.combine_chunks() if isinstance(column, pa.ChunkedArray) else column


class NestedView:
    """Resolves nested paths on a table and keeps what it resolved."""

    def __init__(self, table: pa.Table) -> None:
        self._table = table
        self.columns: dict[tuple[Steps, bool], NestedColumn | Missing] = {}

    def column(self, steps: Steps, *, ignore_case: bool = False) -> NestedColumn | Missing:
        """The elements at the path, or why it cannot be read."""
        key = (steps, ignore_case)
        if key not in self.columns:
            self.columns[key] = self._resolve(steps, ignore_case)
        return self.columns[key]

    def _root(self, name: str, ignore_case: bool) -> _State | Missing:
        names = self._table.column_names
        found = name if name in names else None
        if found is None and ignore_case:
            found = next((n for n in names if n.lower() == name.lower()), None)
        if found is None:
            return Missing(f"parent column {name!r} is missing from the dataset")
        return _State(_combined(self._table[found]))

    def _resolve(self, steps: Steps, ignore_case: bool) -> NestedColumn | Missing:
        first = steps[0]
        if first is None:
            return Missing("a path cannot start with list items")
        state = self._root(first, ignore_case)
        for index, step in enumerate(steps[1:], start=1):
            if isinstance(state, Missing):
                return state
            last = index == len(steps) - 1
            state = (
                _enter_items(state)
                if step is None
                else _enter_field(state, step, ignore_case=ignore_case, last=last)
            )
        if isinstance(state, Missing):
            return state
        return NestedColumn(state.values, state.present, state.rows)


def expand_nested(
    table: pa.Table, obj: SchemaObject, *, max_depth: int = MAX_DEPTH, ignore_case: bool = False
) -> NestedView:
    """A view with every nested path of the object resolved on the table."""
    view = NestedView(table)
    for path in property_paths(obj, max_depth=max_depth):
        if path.depth > 1 and not path.truncated:
            view.column(path.steps, ignore_case=ignore_case)
    return view
