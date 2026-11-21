"""Merge the services' OpenAPI documents into one (PLAT-06, D-176, D-177).

Standard library only, and no import of the platform's packages, so the gateway stays
independent of service code; the repo test loads this file by path and runs the same merge over
the four in-process documents, so a collision fails CI before any deploy.

Collision policy (D-177):
- every path must sit under its owning service's ``/api/v1/<svc>/`` prefix;
- a path or an ``operationId`` that two services both declare is an error;
- components with the same name and canonically identical JSON are kept once (for example the
  shared ``Problem`` model and the ``bearerAuth`` scheme); the same name with different content
  is an error. A collision is fixed by renaming the model in the service, never here.

Every error names both owners. Paths are already prefixed by the services, so nothing is
rewritten and every ``$ref`` stays valid.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
GATEWAY_OWNER = "gateway"
BEARER_SCHEME: dict[str, Any] = {"type": "http", "scheme": "bearer"}


class MergeError(Exception):
    """Two services' documents cannot be merged into one; the message names both owners."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def merge_specs(specs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Merge ``{"identity": {...}, "contracts": {...}, ...}`` into one OpenAPI 3.1 document.

    A spec with no paths contributes its components only. Raises ``MergeError`` on any
    collision; never returns a partial document.
    """
    paths: dict[str, Any] = {}
    tags: list[dict[str, Any]] = []
    components: dict[str, dict[str, Any]] = {
        "securitySchemes": {"bearerAuth": copy.deepcopy(BEARER_SCHEME)}
    }
    owner_path: dict[str, str] = {}
    owner_op: dict[str, str] = {}
    owner_comp: dict[tuple[str, str], tuple[str, str]] = {
        ("securitySchemes", "bearerAuth"): (GATEWAY_OWNER, _canonical(BEARER_SCHEME))
    }
    owner_tag: dict[str, tuple[str, str]] = {}

    for svc, spec in specs.items():
        prefix = f"/api/v1/{svc}/"
        for path, item in (spec.get("paths") or {}).items():
            if not path.startswith(prefix):
                raise MergeError(f"path {path} of {svc} is outside its prefix {prefix}")
            if path in owner_path:
                raise MergeError(f"path {path} in {owner_path[path]} and {svc}")
            for method, operation in item.items():
                if method not in HTTP_METHODS or not isinstance(operation, Mapping):
                    continue
                operation_id = operation.get("operationId")
                if operation_id is None:
                    continue
                if operation_id in owner_op:
                    raise MergeError(
                        f"operationId {operation_id} in {owner_op[operation_id]} and {svc}"
                    )
                owner_op[operation_id] = svc
            paths[path] = copy.deepcopy(item)
            owner_path[path] = svc

        for section, entries in (spec.get("components") or {}).items():
            for name, body in entries.items():
                key = (section, name)
                canon = _canonical(body)
                if key in owner_comp:
                    first_owner, first_canon = owner_comp[key]
                    if first_canon != canon:
                        raise MergeError(
                            f"component {section}/{name} differs: {first_owner} vs {svc}"
                        )
                    continue
                owner_comp[key] = (svc, canon)
                components.setdefault(section, {})[name] = copy.deepcopy(body)

        for tag in spec.get("tags") or []:
            name = tag["name"]
            canon = _canonical(tag)
            if name in owner_tag:
                first_owner, first_canon = owner_tag[name]
                if first_canon != canon:
                    raise MergeError(f"tag {name} differs: {first_owner} vs {svc}")
                continue
            owner_tag[name] = (svc, canon)
            tags.append(copy.deepcopy(tag))

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Contractum API",
            "description": "Contractum: data contract governance",
            "version": "v1",
        },
        "servers": [{"url": "/"}],
        "paths": paths,
        "tags": tags,
        "components": components,
    }
