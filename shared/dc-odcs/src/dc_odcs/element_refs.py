"""References inside one ODCS document: elements, servers, people and the team (D-153, VAL-04/05).

Pure and I/O free. ``reference_inputs`` lists every reference string a contract makes; the
resolution of the ones the document itself can answer (an element named by an SLA, a relationship
or a quality check, and a server named by the server convention) is ``resolve_internal``. People,
teams and contract links need another service and are resolved by the caller.

Element grammar (ODCS 3.1/3.2 JSON Schema ``$defs``):

* shorthand ``table.column[.nested]``: resolved by *name*;
* fully qualified ``schema/<id>/properties/<id>[/properties/<id>...]``: resolved by *id*, with an
  optional ``https://.../file.yaml#`` prefix, which points at another file and is ``unverifiable``;
* a bare name is a schema object (or, inside a quality check of that object, a property of it);
* any other text cannot be interpreted and is ``unverifiable``.

Server references: ODCS has no field that references ``servers[]`` from elsewhere, so the platform
recognises the convention of a ``customProperties`` entry whose ``property`` is ``server`` (a
string or a list of strings) on schema objects, properties, quality rules and SLA properties.

Only ``not_found`` may fail a rule; ``unverifiable`` is information (D-153). The walk is iterative
(an explicit stack) and capped, so a hostile document cannot make it unbounded.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

RefState = Literal["found", "not_found", "unverifiable"]

# How many reference occurrences one extraction collects (a bound on work, not on the report).
MAX_INPUT_REFS = 20_000
# The default per-map cap of ``resolve_internal``; callers pass their own setting.
DEFAULT_MAX_REFS = 200
SERVER_PROPERTY = "server"

_NAME = r"[A-Za-z_][A-Za-z0-9_\-]*"
_SHORTHAND = re.compile(rf"^{_NAME}(\.{_NAME})+$")
_BARE = re.compile(rf"^{_NAME}$")
_QUALIFIED = re.compile(r"^/?schema/([^/]+)((?:/properties/[^/]+)*)$")
_PROPERTY_STEP = re.compile(r"/properties/([^/]+)")
# ``file.yaml#...`` or ``https://host/file.yaml#...``: the element lives in another document.
_EXTERNAL = re.compile(r"^[^#\s]+#")
_PRECEDENCE = {"found": 0, "unverifiable": 1, "not_found": 2}


@dataclass(frozen=True)
class ReferenceInputs:
    """Every reference string of a document, as written, in document order, distinct.

    ``element_uses`` pairs an element string with the index of the schema object whose quality
    check names it (``None`` elsewhere); a bare name in a quality check is a property of that
    object.
    """

    element_uses: tuple[tuple[str, int | None], ...]
    server_refs: tuple[str, ...]
    user_names: tuple[str, ...]
    team_name: str | None

    @property
    def element_refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(text for text, _ in self.element_uses))


class _Collector:
    def __init__(self) -> None:
        self.uses: dict[tuple[str, int | None], None] = {}
        self.servers: dict[str, None] = {}
        self.users: dict[str, None] = {}

    def full(self) -> bool:
        return len(self.uses) + len(self.servers) + len(self.users) >= MAX_INPUT_REFS

    def element(self, value: Any, scope: int | None = None) -> None:
        if isinstance(value, str) and value.strip() and not self.full():
            self.uses[(value.strip(), scope)] = None

    def elements(self, value: Any, scope: int | None = None) -> None:
        if isinstance(value, str):
            self.element(value, scope)
        elif isinstance(value, list):
            for item in value[:MAX_INPUT_REFS]:
                self.element(item, scope)

    def server_property(self, entries: Any) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries[:MAX_INPUT_REFS]:
            if not isinstance(entry, dict) or entry.get("property") != SERVER_PROPERTY:
                continue
            value = entry.get("value")
            names = [value] if isinstance(value, str) else value if isinstance(value, list) else []
            for name in names[:MAX_INPUT_REFS]:
                if isinstance(name, str) and name.strip() and not self.full():
                    self.servers[name.strip()] = None


def _mappings(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value[:MAX_INPUT_REFS]:
            if isinstance(item, dict):
                yield item


def _node_references(node: dict[str, Any], scope: int | None, out: _Collector) -> None:
    """Relationships, quality checks and server properties of a schema object or property."""
    for relationship in _mappings(node.get("relationships")):
        out.elements(relationship.get("from"))
        out.elements(relationship.get("to"))
    out.server_property(node.get("customProperties"))
    for quality in _mappings(node.get("quality")):
        arguments = quality.get("arguments")
        if isinstance(arguments, dict):
            out.elements(arguments.get("properties"), scope)
        out.server_property(quality.get("customProperties"))


def reference_inputs(document: Any) -> ReferenceInputs:
    """Collect the references of ``document``. Total: any shape is accepted, junk is ignored."""
    out = _Collector()
    team_name: str | None = None
    if not isinstance(document, dict):
        return ReferenceInputs((), (), (), None)

    for sla in _mappings(document.get("slaProperties")):
        element = sla.get("element")
        if isinstance(element, str):
            for part in element.split(","):
                out.element(part)
        out.server_property(sla.get("customProperties"))
    default_element = document.get("slaDefaultElement")
    if isinstance(default_element, str):
        for part in default_element.split(","):
            out.element(part)
    for relationship in _mappings(document.get("relationships")):
        out.elements(relationship.get("from"))
        out.elements(relationship.get("to"))

    schema = document.get("schema")
    objects = schema if isinstance(schema, list) else []
    for index, obj in enumerate(objects[:MAX_INPUT_REFS]):
        if not isinstance(obj, dict):
            continue
        _node_references(obj, index, out)
        stack: list[Any] = [obj.get("properties")]
        while stack and not out.full():
            for prop in _mappings(stack.pop()):
                _node_references(prop, index, out)
                stack.append(prop.get("properties"))
                items = prop.get("items")
                if isinstance(items, dict):
                    _node_references(items, index, out)
                    stack.append(items.get("properties"))

    team = document.get("team")
    members: Any = None
    if isinstance(team, dict):
        name = team.get("name")
        if isinstance(name, str) and name.strip():
            team_name = name.strip()
        members = team.get("members")
    elif isinstance(team, list):
        members = team
    for member in _mappings(members):
        username = member.get("username")
        if isinstance(username, str) and username.strip() and not out.full():
            out.users[username.strip()] = None

    return ReferenceInputs(
        element_uses=tuple(out.uses),
        server_refs=tuple(out.servers),
        user_names=tuple(out.users),
        team_name=team_name,
    )


# -- resolution -----------------------------------------------------------------------------------


def _property_children(prop: dict[str, Any]) -> list[dict[str, Any]]:
    children = list(_mappings(prop.get("properties")))
    items = prop.get("items")
    if isinstance(items, dict):
        children.extend(_mappings(items.get("properties")))
    return children


def _find_property(
    properties: list[dict[str, Any]], steps: list[str], key: str
) -> dict[str, Any] | None:
    """Walk ``steps`` through nested properties, matching each step on ``key`` (name or id)."""
    current = properties
    found: dict[str, Any] | None = None
    for step in steps:
        found = next((p for p in current if p.get(key) == step), None)
        if found is None:
            return None
        current = _property_children(found)
    return found


class _Index:
    def __init__(self, document: dict[str, Any]) -> None:
        schema = document.get("schema")
        self.objects = list(_mappings(schema))
        self.by_name: dict[str, dict[str, Any]] = {}
        self.by_id: dict[str, dict[str, Any]] = {}
        for obj in self.objects:
            if isinstance(obj.get("name"), str):
                self.by_name.setdefault(obj["name"], obj)
            if isinstance(obj.get("id"), str):
                self.by_id.setdefault(obj["id"], obj)

    def properties(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        return list(_mappings(obj.get("properties")))

    def resolve(self, text: str, scope: int | None) -> RefState:
        value = text.strip()
        if _EXTERNAL.match(value) and not value.startswith("#"):
            return "unverifiable"  # another file: not ours to check
        value = value.removeprefix("#")
        qualified = _QUALIFIED.match(value)
        if qualified is not None:
            obj = self.by_id.get(qualified.group(1))
            if obj is None:
                return "not_found"
            steps = _PROPERTY_STEP.findall(qualified.group(2))
            if not steps:
                return "found"
            found = _find_property(self.properties(obj), steps, "id")
            return "found" if found is not None else "not_found"
        if _SHORTHAND.match(value) is not None:
            table, *steps = value.split(".")
            obj = self.by_name.get(table)
            if obj is None:
                return "not_found"
            found = _find_property(self.properties(obj), steps, "name")
            return "found" if found is not None else "not_found"
        if _BARE.match(value) is not None:
            if scope is not None and scope < len(self.objects):
                owner = self.objects[scope]
                known = _find_property(self.properties(owner), [value], "name")
                return "found" if known is not None else "not_found"
            return "found" if value in self.by_name else "not_found"
        return "unverifiable"


def _worst(left: RefState | None, right: RefState) -> RefState:
    if left is None or _PRECEDENCE[right] > _PRECEDENCE[left]:
        return right
    return left


def resolve_internal(
    document: Any, inputs: ReferenceInputs, *, max_refs: int = DEFAULT_MAX_REFS
) -> dict[str, dict[str, RefState]]:
    """Resolve element and server references against the document itself, without I/O.

    Returns ``{"elements": {...}, "servers": {...}}`` keyed by the reference as written, with
    tri-state values only. Each map resolves at most ``max_refs`` distinct references; the rest
    are ``unverifiable`` (they are still keys). A bare name used in the quality checks of two
    objects resolves to its worst state (``not_found`` over ``unverifiable`` over ``found``).
    """
    doc: dict[str, Any] = document if isinstance(document, dict) else {}
    index = _Index(doc)
    resolved_texts = set(inputs.element_refs[:max_refs])
    elements: dict[str, RefState] = {}
    for text in inputs.element_refs:
        elements[text] = "found" if text in resolved_texts else "unverifiable"
    states: dict[str, RefState] = {}
    for text, scope in inputs.element_uses:
        if text in resolved_texts:
            states[text] = _worst(states.get(text), index.resolve(text, scope))
    elements.update(states)

    declared = {
        server["server"]
        for server in _mappings(doc.get("servers"))
        if isinstance(server.get("server"), str)
    }
    servers: dict[str, RefState] = {}
    for position, name in enumerate(inputs.server_refs):
        if position >= max_refs:
            servers[name] = "unverifiable"
        else:
            servers[name] = "found" if name in declared else "not_found"
    return {"elements": elements, "servers": servers}


__all__ = [
    "DEFAULT_MAX_REFS",
    "MAX_INPUT_REFS",
    "SERVER_PROPERTY",
    "ReferenceInputs",
    "RefState",
    "reference_inputs",
    "resolve_internal",
]
