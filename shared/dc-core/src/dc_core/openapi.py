"""One OpenAPI post-processor for every service (PLAT-06, D-177, D-179).

``create_app`` installs ``build_openapi`` as ``app.openapi``, so every service document:

* tags each operation (the service name when the route declares no tag);
* declares its security from the route's own ``PolicyMeta``: ``[]`` for ``public()`` routes,
  ``[{"bearerAuth": []}]`` for every ``require()`` route (never hand-written, T-7-06);
* documents every 4xx and 5xx response as ``application/problem+json`` with the shared
  ``Problem`` schema (the services answer RFC 9457 bodies, ``dc_core.problems``), keeping the
  route's own description, and drops FastAPI's ``HTTPValidationError``/``ValidationError``;
* declares the statuses every route can produce (400 and 429 everywhere; 401, 403 and 503 on
  guarded routes; 404 with a path parameter; 422 with parameters or a body; 413 with a body).

``PROBLEM_SCHEMA`` and ``BEARER_SCHEME`` are module data so every service emits identical bytes
and the gateway merge de-duplicates them (D-177). Changing either changes every service
document and the merged one. Route-specific statuses (409, 412, 428, 406, ...) stay each
route's own ``responses=`` declaration. No ``default`` response is added: it would let any
undocumented status pass a conformance check.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

from dc_core.policy import find_policy
from dc_core.problems import PROBLEM_MEDIA_TYPE

BEARER_SCHEME_NAME = "bearerAuth"
BEARER_SCHEME: dict[str, Any] = {"type": "http", "scheme": "bearer"}
PROBLEM_SCHEMA_NAME = "Problem"
PROBLEM_REF = f"#/components/schemas/{PROBLEM_SCHEMA_NAME}"
PROBLEM_SCHEMA: dict[str, Any] = {
    "title": "Problem",
    "description": "RFC 9457 problem details. Extensions such as report, run_id and reasons "
    "may appear.",
    "type": "object",
    "properties": {
        "type": {"type": "string", "description": "urn:dc:problem:<slug>"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "request_id": {"type": "string"},
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pointer": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["pointer", "detail"],
            },
        },
    },
    "required": ["type", "title", "status"],
    "additionalProperties": True,
}

_FASTAPI_VALIDATION_SCHEMAS = ("HTTPValidationError", "ValidationError")
_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

DEFAULT_DESCRIPTIONS: dict[str, str] = {
    "400": "Bad request (for example an encoded slash or NUL in the path)",
    "401": "Missing, expired or invalid credentials",
    "403": "The caller may see the resource but not perform the operation",
    "404": "Not found, or not visible to the caller",
    "413": "Request body too large",
    "422": "Validation error",
    "429": "Too many requests",
    "503": "A dependency (such as the authorization service) is unavailable",
}


def _problem_content() -> dict[str, Any]:
    return {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": PROBLEM_REF}}}


def _status_key(key: str) -> int | None:
    try:
        return int(key)
    except ValueError:
        return None


def _common_statuses(operation: dict[str, Any], *, path: str, public: bool) -> list[str]:
    params = operation.get("parameters") or []
    has_body = "requestBody" in operation
    has_path_param = "{" in path or any(p.get("in") == "path" for p in params)
    statuses = ["400", "429"]
    if not public:
        statuses += ["401", "403", "503"]
    if has_path_param:
        statuses.append("404")
    if params or has_body:
        statuses.append("422")
    if has_body:
        statuses.append("413")
    return statuses


def _process_operation(operation: dict[str, Any], *, path: str, public: bool, service: str) -> None:
    if not operation.get("tags"):
        operation["tags"] = [service]
    operation["security"] = [] if public else [{BEARER_SCHEME_NAME: []}]

    responses: dict[str, Any] = operation.setdefault("responses", {})
    for status in _common_statuses(operation, path=path, public=public):
        responses.setdefault(status, {"description": DEFAULT_DESCRIPTIONS[status]})
    for key, response in responses.items():
        code = _status_key(key)
        is_error = (code is not None and code >= 400) or key.upper() in {"4XX", "5XX"}
        if is_error:
            response.setdefault("description", DEFAULT_DESCRIPTIONS.get(key, "Error"))
            response["content"] = _problem_content()
    operation["responses"] = dict(
        sorted(responses.items(), key=lambda item: (_status_key(item[0]) or 999, item[0]))
    )


def build_openapi(app: FastAPI, *, service: str) -> dict[str, Any]:
    """Generate the service document with tags, security and problem responses."""
    # FastAPI's own generator passes every app-level setting (summary, terms, contact, license,
    # webhooks, tags, servers, separate_input_output_schemas, external_docs). It caches its raw
    # result on ``app.openapi_schema``, so start from an empty cache and put the old value back.
    cached, app.openapi_schema = app.openapi_schema, None
    try:
        schema = copy.deepcopy(FastAPI.openapi(app))
    finally:
        app.openapi_schema = cached
    paths: dict[str, dict[str, Any]] = schema.setdefault("paths", {})

    for rc in iter_route_contexts(app.routes):
        if not isinstance(rc.original_route, APIRoute):
            continue
        path = rc.path_format or ""
        item = paths.get(path)
        if item is None:
            continue
        meta = find_policy(rc.dependant)
        # A route with no policy cannot exist (PolicyRouter, walker); treat it as guarded.
        public = bool(meta is not None and meta.public)
        for method in rc.methods or ():
            operation = item.get(method.lower())
            if operation is not None:
                _process_operation(operation, path=path, public=public, service=service)

    # Operations no APIRoute claimed (should not happen) still get the guarded defaults.
    for path, item in paths.items():
        for method, operation in item.items():
            if method in _METHODS and "security" not in operation:
                _process_operation(operation, path=path, public=False, service=service)

    components: dict[str, Any] = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})[BEARER_SCHEME_NAME] = dict(BEARER_SCHEME)
    schemas: dict[str, Any] = components.setdefault("schemas", {})
    schemas[PROBLEM_SCHEMA_NAME] = json.loads(json.dumps(PROBLEM_SCHEMA))
    removed = {name: schemas.pop(name) for name in _FASTAPI_VALIDATION_SCHEMAS if name in schemas}
    serialized = json.dumps(schema)
    for name, body in removed.items():
        if f'"#/components/schemas/{name}"' in serialized:
            schemas[name] = body  # still referenced by a route's own model: keep it valid
    components["schemas"] = dict(sorted(schemas.items()))
    return schema
