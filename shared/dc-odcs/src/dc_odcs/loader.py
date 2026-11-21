"""Strict YAML and JSON loading.

The loaders never silently retype or merge data (research Pitfall 3, threats T-01-09/T-01-10):

* YAML 1.1 implicit booleans (``NO``, ``yes``, ``on``) and timestamps stay strings; only the
  lower-case ``true``/``false`` literals are booleans.
* YAML 1.1 numeric look-alikes stay strings too: ``0755`` (octal), ``0b11``, ``0x1F``, ``1_000``
  and the base-60 ``12:30`` are not numbers. Only plain decimal integers (``0``, ``-5``, ``+1``,
  ``42``; no leading zeros) and decimal floats (``1.5``, ``.5``, ``-2.5e-3``, ``.inf``,
  ``.nan``) are numbers, as in the YAML 1.2 core schema.
* Duplicate mapping keys, anchors and aliases are rejected. The alias scan runs on the event
  stream, before any node is expanded, so an alias bomb is never constructed.
* JSON duplicate keys and the non-standard ``NaN``/``Infinity`` constants are rejected.
* Structural nesting deeper than ``MAX_NESTING_DEPTH`` is refused before it is parsed into
  objects: PyYAML's scanner is super-linear in flow nesting depth (a 1 MiB ``[[[...]]]`` ran for
  minutes), so the YAML event stream is aborted at the first over-deep node and JSON is
  pre-scanned linearly.

Both loaders raise ``ValueError`` subclasses or ``yaml.YAMLError``; ``dc_odcs.gate`` maps them to
``ContractRejected``.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml
from yaml.constructor import ConstructorError

# Maximum nesting depth of mappings and sequences (the root collection is depth 1). Each level of
# a nested ODCS object costs two (a ``properties`` list and the property mapping), so 128 allows
# about 60 nested object levels; the deepest real contract and the whole ODCS schema stay under
# 20. A fixed constant, not a setting: it is a safety bound, not a tuning knob.
MAX_NESTING_DEPTH = 128

_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_BOOL_TAG = "tag:yaml.org,2002:bool"
_MERGE_TAG = "tag:yaml.org,2002:merge"
_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"

# YAML 1.2 core-style numbers. PyYAML's 1.1 patterns also accept leading-zero octal, 0b/0x,
# ``_`` separators and base-60 (``12:30``), which silently retype strings (WR-08).
_INT_PATTERN = re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$")
_FLOAT_PATTERN = re.compile(
    r"^(?:[-+]?[0-9]+\.[0-9]*(?:[eE][-+][0-9]+)?|\.[0-9]+(?:[eE][-+][0-9]+)?"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
)


class StrictCLoader(yaml.CSafeLoader):  # type: ignore[misc]
    """libyaml-backed safe loader without YAML 1.1 implicit typing surprises."""


# yaml_implicit_resolvers is a class-level dict shared with the base class; rebuild it so the
# removal below does not leak into yaml.CSafeLoader itself.
StrictCLoader.yaml_implicit_resolvers = {
    first_char: [
        (tag, regexp)
        for (tag, regexp) in resolvers
        if tag not in (_TIMESTAMP_TAG, _BOOL_TAG, _INT_TAG, _FLOAT_TAG)
    ]
    for first_char, resolvers in yaml.CSafeLoader.yaml_implicit_resolvers.items()
}
StrictCLoader.add_implicit_resolver(_BOOL_TAG, re.compile(r"^(?:true|false)$"), list("tf"))
StrictCLoader.add_implicit_resolver(_INT_TAG, _INT_PATTERN, list("-+0123456789"))
StrictCLoader.add_implicit_resolver(_FLOAT_TAG, _FLOAT_PATTERN, list("-+0123456789."))


def _construct_unique_mapping(
    loader: StrictCLoader, node: yaml.MappingNode, deep: bool = False
) -> Any:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        if key_node.tag == _MERGE_TAG:
            raise ConstructorError(
                None, None, "merge keys ('<<') are not supported", key_node.start_mark
            )
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in seen
        except TypeError:
            # Unhashable key (e.g. a list); the base constructor reports it with a proper mark.
            continue
        if duplicate:
            raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.CSafeLoader.construct_mapping(loader, node, deep)


StrictCLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class YamlAliasError(ValueError):
    """Raised when the document uses an anchor or an alias."""


class NestingTooDeepError(ValueError):
    """Raised when a document nests mappings or sequences deeper than ``MAX_NESTING_DEPTH``.

    The message is fixed and carries no document value or position.
    """

    def __init__(self) -> None:
        super().__init__(f"nesting is too deep (at most {MAX_NESTING_DEPTH} levels are allowed)")


def load_yaml(text: str) -> Any:
    """Parse YAML text strictly. Raises ``yaml.YAMLError`` or ``YamlAliasError``."""
    # Alias/anchor and depth pre-scan on the event stream: nothing is expanded before this
    # passes, and the stream is abandoned at the first over-deep collection, so the scanner never
    # reaches the depths where it is super-linear.
    depth = 0
    for event in yaml.parse(text, Loader=yaml.CSafeLoader):
        if isinstance(event, yaml.CollectionStartEvent):
            depth += 1
            if depth > MAX_NESTING_DEPTH:
                raise NestingTooDeepError
        elif isinstance(event, yaml.CollectionEndEvent):
            depth -= 1
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            mark = event.start_mark
            raise YamlAliasError(
                f"YAML aliases are not allowed (line {mark.line + 1}, column {mark.column + 1})"
            )
    return yaml.load(text, Loader=StrictCLoader)  # noqa: S506 - StrictCLoader is a safe loader


class JsonStrictError(ValueError):
    """Raised for JSON the platform refuses (duplicate keys, NaN/Infinity)."""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonStrictError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise JsonStrictError(f"{name} is not a valid JSON number")


_JSON_TOKEN = re.compile(r'["\[\]{}]')
_JSON_STRING_END = re.compile(r'[^"\\]*(?:\\.[^"\\]*)*"', re.DOTALL)


def _check_json_depth(text: str) -> None:
    """Linear, string-aware bracket-depth scan; never builds a value.

    An unterminated string stops the scan: the parser reports that error itself.
    """
    depth = 0
    position = 0
    while (match := _JSON_TOKEN.search(text, position)) is not None:
        char = match.group()
        if char == '"':
            end = _JSON_STRING_END.match(text, match.end())
            if end is None:
                return
            position = end.end()
            continue
        if char in "[{":
            depth += 1
            if depth > MAX_NESTING_DEPTH:
                raise NestingTooDeepError
        else:
            depth -= 1
        position = match.end()


def load_json(text: str) -> Any:
    """Parse JSON text strictly. Raises ``json.JSONDecodeError`` or ``JsonStrictError``.

    ``NestingTooDeepError`` (a ``ValueError``) is raised before parsing for over-deep input.
    """
    _check_json_depth(text)
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
