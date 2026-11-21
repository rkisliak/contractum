"""Links from one contract to another, found in a parsed ODCS document (CONT-04, D-54).

ODCS has no native "this contract depends on that contract" field, so the platform recognises two
conventions at any depth of the document:

* ``relationships[].to`` (a string or a list of strings) or ``authoritativeDefinitions[].url``
  containing ``/api/v1/contracts/contracts/<uuid>``: the target is named by its platform id
  (``contract_uuid``);
* ``relationships[].to`` or ``authoritativeDefinitions[].url`` that is a file reference
  ``<odcs-id>.yaml#...`` (or ``.yml``): the target is named by its ODCS id (``odcs_id``).

The walk is iterative (an explicit stack, never recursion, so a deeply nested document cannot
exhaust the interpreter stack) and capped, so a document cannot make the extraction unbounded.
Detection rules are data (``_UUID_URL``, ``_FILE_REF``): a team that links differently gets a new
rule here, nothing else changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

MAX_REFS = 1000

# The longest version text contracts and validation exchange: a baseline's ``semver`` and a
# link's pinned version (CR-02, WR-01). Contracts accepts any strict SemVer as a document
# ``version`` (build metadata has no length limit) and links of up to this many characters, so a
# pinned version always fits; contracts drops a longer baseline ``semver`` (display only:
# validation then derives it from the baseline document). Both services import this one value,
# so their caps cannot drift apart.
MAX_VERSION_CHARS = 2048

RefKind = Literal["contract_uuid", "odcs_id"]

_PLATFORM_PATH = "/api/v1/contracts/contracts/"
_UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_UUID_URL = re.compile(re.escape(_PLATFORM_PATH) + "(" + _UUID + ")")
# ``:`` is allowed so that URN-style ODCS ids (``urn:dc:orders:...``) are recognised.
_FILE_REF = re.compile(r"^(?:https?://)?[A-Za-z0-9._:\-/]+\.ya?ml#")
_FILE_SUFFIX = re.compile(r"\.ya?ml$")


@dataclass(frozen=True)
class ContractRef:
    """One link: what it names (``kind``/``value``) and where it sits (``pointer``)."""

    kind: RefKind
    value: str
    pointer: str


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _classify(value: str) -> tuple[RefKind, str] | None:
    found = _UUID_URL.search(value)
    if found is not None:
        return "contract_uuid", found.group(1).lower()
    if _FILE_REF.match(value) is not None:
        path = value.split("#", 1)[0]
        name = _FILE_SUFFIX.sub("", path.rsplit("/", 1)[-1])
        if name:
            return "odcs_id", name
    return None


def _link_values(node: dict[str, Any]) -> list[tuple[str, str]]:
    """``(pointer relative to this mapping, value)`` for each link-bearing string in the mapping."""
    out: list[tuple[str, str]] = []
    relationships = node.get("relationships")
    if isinstance(relationships, list):
        for index, entry in enumerate(relationships):
            if not isinstance(entry, dict):
                continue
            target = entry.get("to")
            base = f"/relationships/{index}/to"
            if isinstance(target, str):
                out.append((base, target))
            elif isinstance(target, list):
                out.extend(
                    (f"{base}/{i}", item) for i, item in enumerate(target) if isinstance(item, str)
                )
    definitions = node.get("authoritativeDefinitions")
    if isinstance(definitions, list):
        for index, entry in enumerate(definitions):
            if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                out.append((f"/authoritativeDefinitions/{index}/url", entry["url"]))
    return out


def extract_contract_refs(
    document: Any, *, max_refs: int = MAX_REFS, distinct: Literal["target", "raw"] = "target"
) -> list[ContractRef]:
    """The distinct contract links of ``document``, in document order, at most ``max_refs``.

    With ``distinct="target"`` (the reference index and delete guard) duplicates by
    ``(kind, value)`` collapse and keep the first pointer. With ``distinct="raw"`` (link facts)
    only identical link strings collapse, so two links to one contract that pin different
    versions each get a fact (IN-04). Total: any shape of ``document`` is accepted and values that
    match no rule are ignored.
    """
    refs: list[ContractRef] = []
    seen: set[object] = set()
    # Each stack item is (node, parent link, key): the pointer is only built for a match, by
    # following the parent chain, so a very deep document costs no quadratic string building.
    stack: list[tuple[Any, tuple[Any, ...] | None, str]] = [(document, None, "")]
    while stack and len(refs) < max_refs:
        node, parent, key = stack.pop()
        link = (parent, key)
        if isinstance(node, dict):
            prefix: str | None = None
            for relative, value in _link_values(node):
                classified = _classify(value)
                if classified is None:
                    continue
                identity: object = value if distinct == "raw" else classified
                if identity in seen:
                    continue
                if prefix is None:
                    prefix = _pointer(link)
                seen.add(identity)
                refs.append(ContractRef(classified[0], classified[1], prefix + relative))
                if len(refs) >= max_refs:
                    return refs
            # reversed, so the first child is popped first (document order)
            for child_key, child in reversed(list(node.items())):
                if isinstance(child, dict | list):
                    stack.append((child, link, _escape(str(child_key))))
        elif isinstance(node, list):
            for index in range(len(node) - 1, -1, -1):
                child = node[index]
                if isinstance(child, dict | list):
                    stack.append((child, link, str(index)))
    return refs


def _pointer(link: tuple[Any, ...]) -> str:
    tokens: list[str] = []
    current: tuple[Any, ...] | None = link
    while current is not None and current[0] is not None:
        parent, key = current
        tokens.append(key)
        current = parent
    return "".join(f"/{token}" for token in reversed(tokens))


__all__ = ["MAX_REFS", "MAX_VERSION_CHARS", "ContractRef", "RefKind", "extract_contract_refs"]
