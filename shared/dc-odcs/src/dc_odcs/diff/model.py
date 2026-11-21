"""Result types of the contract diff: ``Change``, ``DiffResult`` and the value allow-list."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

ChangeClass = Literal["breaking", "warning", "info"]
JsonScalar = str | int | float | bool | None

MAX_DIFF_CHANGES: Final = 10_000
MAX_VALUE_CHARS: Final = 200
MAX_VALUE_ITEMS: Final = 50

# Only these attributes may carry before/after values in a Change. Everything else (descriptions,
# examples, quality SQL, custom property values, contacts) is document free text and never leaves
# the classifier (T-6-02). ``type`` counts only on a server element (see ``value_allowed``).
ALLOWED_VALUE_ATTRIBUTES: Final = frozenset(
    {
        "logicalType",
        "physicalType",
        "required",
        "unique",
        "primaryKey",
        "primaryKeyPosition",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "enum",
        "type",
    }
)


def value_allowed(element_kind: str, attribute: str | None) -> bool:
    """Whether a before/after value may be recorded for this element kind and attribute."""
    if attribute is None or attribute not in ALLOWED_VALUE_ATTRIBUTES:
        return False
    return attribute != "type" or element_kind == "server"


def _scalar(value: Any) -> JsonScalar | None:
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if value is None or isinstance(value, bool | int | float):
        return value
    return None


def capped_value(value: Any) -> JsonScalar | list[JsonScalar]:
    """A scalar (strings capped), or a capped list of scalars; anything else becomes ``None``."""
    if isinstance(value, list):
        items = value[:MAX_VALUE_ITEMS]
        if all(isinstance(item, str | int | float | bool) or item is None for item in items):
            return [_scalar(item) for item in items]
        return None
    return _scalar(value)


@dataclass(frozen=True)
class Change:
    """One classified difference between two contract versions."""

    code: str
    cls: ChangeClass
    element_kind: str
    path: str
    pointer_before: str | None
    pointer_after: str | None
    attribute: str | None
    before: JsonScalar | list[JsonScalar]
    after: JsonScalar | list[JsonScalar]

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "class": self.cls,
            "element_kind": self.element_kind,
            "path": self.path,
            "pointer_before": self.pointer_before,
            "pointer_after": self.pointer_after,
            "attribute": self.attribute,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class DiffResult:
    """The ordered changes, the true per-class counts and whether the list was cut short."""

    changes: tuple[Change, ...]
    counts: Mapping[str, int]
    changes_total: int
    truncated: bool
    classifier_version: str

    def as_binding(self) -> list[dict[str, Any]]:
        """JSON-native list for the CEL ``changes`` binding."""
        return [change.to_json() for change in self.changes]


__all__ = [
    "ALLOWED_VALUE_ATTRIBUTES",
    "MAX_DIFF_CHANGES",
    "Change",
    "ChangeClass",
    "DiffResult",
    "JsonScalar",
    "capped_value",
    "value_allowed",
]
