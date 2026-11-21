"""Read-only accessors over an ODCS data contract document.

The SDK never imports the server's ODCS package (D-05). A contract is a plain JSON-native dict;
this module turns the parts the engine needs into small frozen values. The readers accept the
shapes of ODCS v3.0.x, v3.1.0 and v3.2.0 (scalar or ``{value}`` enum entries, ``rule`` as the old
name of ``metric``, library arguments nested or as siblings) and never apply schema defaults, so
only what the contract states becomes a check.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from contractum.errors import UsageError

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_NODES = 1_000_000

_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_NOT_AN_OBJECT = "contract document is not a JSON/YAML object"

# Operators of a quality rule, in the order a rule is read when several are present.
_OPERATORS = (
    "mustBe",
    "mustNotBe",
    "mustBeGreaterThan",
    "mustBeGreaterOrEqualTo",
    "mustBeLessThan",
    "mustBeLessOrEqualTo",
    "mustBeBetween",
    "mustNotBeBetween",
)

# Keys of a quality rule that are not metric arguments when they appear beside the metric.
_RULE_KEYS = frozenset(
    {
        "id",
        "name",
        "description",
        "type",
        "rule",
        "metric",
        "unit",
        "severity",
        "dimension",
        "engine",
        "implementation",
        "query",
        "method",
        "arguments",
        "businessImpact",
        "customProperties",
        "authoritativeDefinitions",
        "tags",
        "schedule",
        "scheduler",
        *_OPERATORS,
    }
)


class _JsonNativeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """A ``yaml.SafeLoader`` without the implicit timestamp resolver, so dates stay strings."""


_JsonNativeLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != _TIMESTAMP_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_document(source: bytes | str, *, media_type: str | None = None) -> dict[str, Any]:
    """Parse a contract from JSON or YAML text into a JSON-native dict.

    JSON is used when the media type says JSON or the first non-space character is ``{``,
    otherwise the text is read as YAML with dates kept as strings. Input over 8 MiB, a non-mapping
    root and values that are not JSON-native (dates, non-finite numbers, non-string keys) are
    refused with a UsageError.
    """
    raw = source.encode("utf-8") if isinstance(source, str) else source
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise UsageError(
            f"contract document is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MiB"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UsageError("contract document is not valid UTF-8") from exc
    try:
        if _looks_like_json(text, media_type):
            data = json.loads(text, parse_constant=_refuse_constant)
        else:
            # Derives from yaml.SafeLoader: no arbitrary object construction.
            data = yaml.load(text, Loader=_JsonNativeLoader)  # noqa: S506
    except (ValueError, yaml.YAMLError, RecursionError) as exc:
        raise UsageError(f"contract document could not be parsed ({type(exc).__name__})") from exc
    if not isinstance(data, dict) or not _is_json_native(data):
        raise UsageError(_NOT_AN_OBJECT)
    return data


def _looks_like_json(text: str, media_type: str | None) -> bool:
    if media_type is not None and "json" in media_type.lower():
        return True
    return text.lstrip().startswith("{")


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _is_json_native(root: object) -> bool:
    """Walk the value iteratively; refuse non-JSON types and alias bombs over the node cap."""
    stack: list[object] = [root]
    visited = 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > MAX_DOCUMENT_NODES:
            return False
        if isinstance(node, dict):
            if not all(isinstance(key, str) for key in node):
                return False
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, float):
            if not math.isfinite(node):
                return False
        elif node is not None and not isinstance(node, (str, int, bool)):
            return False
    return True


@dataclass(frozen=True)
class QualityRule:
    """One entry of a ``quality`` list."""

    index: int
    id: str | None = None
    name: str | None = None
    type: str | None = None
    metric: str | None = None
    operator: str | None = None
    threshold: Any = None
    unit: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    severity: str | None = None
    dimension: str | None = None
    engine: str | None = None


@dataclass(frozen=True)
class Property:
    """A property of a schema object, with its nested and item properties."""

    name: str
    physical_name: str | None = None
    logical_type: str | None = None
    physical_type: str | None = None
    required: bool = False
    unique: bool = False
    primary_key: bool = False
    primary_key_position: int | None = None
    classification: str | None = None
    enum_values: tuple[Any, ...] | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    quality: tuple[QualityRule, ...] = ()
    items: Property | None = None
    nested: tuple[Property, ...] = ()

    @property
    def source_name(self) -> str:
        """The column name in a dataset: ``physicalName`` when present, else ``name``."""
        return self.physical_name or self.name


@dataclass(frozen=True)
class SchemaObject:
    """A schema object (a table, a file layout) of the contract."""

    name: str
    physical_name: str | None = None
    properties: tuple[Property, ...] = ()
    quality: tuple[QualityRule, ...] = ()


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _entries(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _enum_values(value: object) -> tuple[Any, ...] | None:
    """Scalars (v3.0/3.1) or ``{value}`` objects (v3.2.0) as a tuple of plain values."""
    if not isinstance(value, list):
        return None
    return tuple(item.get("value") if isinstance(item, dict) else item for item in value)


def _quality_rule(index: int, raw: Mapping[str, Any]) -> QualityRule:
    operator = next((key for key in _OPERATORS if key in raw), None)
    arguments = {key: value for key, value in raw.items() if key not in _RULE_KEYS}
    arguments.update(_mapping(raw.get("arguments")))
    return QualityRule(
        index=index,
        id=_text(raw.get("id")),
        name=_text(raw.get("name")),
        type=_text(raw.get("type")),
        metric=_text(raw.get("metric")) or _text(raw.get("rule")),
        operator=operator,
        threshold=raw[operator] if operator else None,
        unit=_text(raw.get("unit")),
        arguments=arguments,
        severity=_text(raw.get("severity")),
        dimension=_text(raw.get("dimension")),
        engine=_text(raw.get("engine")),
    )


def _quality(value: object) -> tuple[QualityRule, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        _quality_rule(index, item) for index, item in enumerate(value) if isinstance(item, dict)
    )


def _classification(value: object) -> str | None:
    """A classification label, or the numeric level ODCS also allows, as text."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _text(value)


def _position(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _property(index: int, raw: Mapping[str, Any]) -> Property:
    name = _text(raw.get("name")) or _text(raw.get("physicalName")) or f"properties[{index}]"
    items = raw.get("items")
    return Property(
        name=name,
        physical_name=_text(raw.get("physicalName")),
        logical_type=_text(raw.get("logicalType")),
        physical_type=_text(raw.get("physicalType")),
        required=raw.get("required") is True,
        unique=raw.get("unique") is True,
        primary_key=raw.get("primaryKey") is True,
        primary_key_position=_position(raw.get("primaryKeyPosition")),
        classification=_classification(raw.get("classification")),
        enum_values=_enum_values(raw.get("enum")),
        options=_mapping(raw.get("logicalTypeOptions")),
        quality=_quality(raw.get("quality")),
        items=_property(0, items) if isinstance(items, dict) else None,
        nested=_properties(raw.get("properties")),
    )


def _properties(value: object) -> tuple[Property, ...]:
    return tuple(_property(index, item) for index, item in enumerate(_entries(value)))


def schema_objects(doc: Mapping[str, Any]) -> tuple[SchemaObject, ...]:
    """The schema objects of a contract, in document order; empty when there is none."""
    objects = []
    for index, raw in enumerate(_entries(doc.get("schema"))):
        name = _text(raw.get("name")) or _text(raw.get("physicalName")) or f"schema[{index}]"
        objects.append(
            SchemaObject(
                name=name,
                physical_name=_text(raw.get("physicalName")),
                properties=_properties(raw.get("properties")),
                quality=_quality(raw.get("quality")),
            )
        )
    return tuple(objects)


def find_object(doc: Mapping[str, Any], name: str | None = None) -> SchemaObject:
    """Pick the schema object called ``name`` (by name, then physicalName).

    Without a name a contract with exactly one object returns it; several objects raise a
    UsageError that lists them and suggests ``--object``.
    """
    objects = schema_objects(doc)
    names = ", ".join(obj.name for obj in objects)
    if not objects:
        raise UsageError("contract has no schema objects")
    if name is None:
        if len(objects) == 1:
            return objects[0]
        raise UsageError(
            f"contract has several schema objects ({names})",
            hint="choose one with --object NAME",
        )
    for candidates in (
        [obj for obj in objects if obj.name == name],
        [obj for obj in objects if obj.physical_name == name],
    ):
        if candidates:
            return candidates[0]
    raise UsageError(f"contract has no schema object {name!r} (available: {names})")
