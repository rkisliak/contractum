"""Route policies: every route declares ``require(...)`` or ``public()`` (D-07, locks PLAT-04).

Two layers enforce it:

* ``PolicyRouter.add_api_route`` refuses a route without a policy at decoration time, so the
  app fails to start (and so every test fails).
* ``assert_all_routes_have_policy`` walks the *effective* routes of an app as a backstop for
  routes added with ``app.add_api_route``, plain routers, mounts and framework routes.
  FastAPI 0.162 keeps included routers as private ``_IncludedRouter`` objects, so walking
  ``app.routes`` directly sees nothing; the public ``iter_route_contexts`` is used instead and
  the walk must find at least ``min_routes`` API routes (a vacuous pass is a failure).
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, params
from fastapi.routing import APIRoute, iter_route_contexts

from dc_core.nul_guard import NulGuardRoute

POLICY_ATTR = "_dc_policy"


@dataclass(frozen=True)
class PolicyMeta:
    permission: str | None
    public: bool = False
    allow_restricted: bool = False


def mark_policy(fn: Callable[..., Any], meta: PolicyMeta) -> Callable[..., Any]:
    setattr(fn, POLICY_ATTR, meta)
    return fn


def public() -> Any:
    """Explicitly public route: no authentication. Must also be allow-listed in the walker."""

    async def _public() -> None:
        return None

    mark_policy(_public, PolicyMeta(None, public=True))
    return Depends(_public)


def find_policy(dependant: Any) -> PolicyMeta | None:
    """First policy marker found in the dependant's (recursive) sub-dependencies."""
    for dep in dependant.dependencies:
        meta = getattr(dep.call, POLICY_ATTR, None)
        if isinstance(meta, PolicyMeta):
            return meta
        nested = find_policy(dep)
        if nested is not None:
            return nested
    return None


def _depends_in(annotation: Any) -> list[params.Depends]:
    found: list[params.Depends] = []
    if typing.get_origin(annotation) is typing.Annotated:
        for meta in typing.get_args(annotation)[1:]:
            if isinstance(meta, params.Depends):
                found.append(meta)
    return found


def _callable_has_policy(call: Callable[..., Any] | None, seen: set[int]) -> bool:
    if call is None or id(call) in seen:
        return False
    seen.add(id(call))
    if isinstance(getattr(call, POLICY_ATTR, None), PolicyMeta):
        return True
    try:
        sig = inspect.signature(call, eval_str=True)
    except (TypeError, ValueError, NameError):
        try:
            sig = inspect.signature(call)
        except (TypeError, ValueError):
            return False
    for param in sig.parameters.values():
        candidates = _depends_in(param.annotation)
        if isinstance(param.default, params.Depends):
            candidates.append(param.default)
        if any(_callable_has_policy(d.dependency, seen) for d in candidates):
            return True
    return False


def _depends_list_has_policy(deps: Sequence[params.Depends] | None) -> bool:
    return any(_callable_has_policy(d.dependency, set()) for d in deps or ())


class PolicyRouter(APIRouter):
    """APIRouter that refuses routes with no declared policy.

    Its routes are ``NulGuardRoute`` by default, so a NUL or lone surrogate in a query parameter or
    a parsed JSON body is a 422 on every service route (``dc_core.nul_guard``).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("route_class", NulGuardRoute)
        super().__init__(*args, **kwargs)

    def add_api_route(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        declared = (
            _depends_list_has_policy(kwargs.get("dependencies"))
            or _depends_list_has_policy(self.dependencies)
            or _callable_has_policy(endpoint, set())
        )
        if not declared:
            raise RuntimeError(f"PolicyRouter refused: route {path} has no policy")
        super().add_api_route(path, endpoint, **kwargs)


def assert_all_routes_have_policy(
    app: FastAPI,
    *,
    module: str,
    allowed_non_api: frozenset[str],
    allowed_public: frozenset[tuple[str, str]],
    min_routes: int = 1,
    known_permissions: frozenset[str] | None = None,
) -> None:
    """Fail (one AssertionError, sorted by path) if any reachable route lacks a policy.

    When ``known_permissions`` is given, every non-public route must also declare a permission
    that is a member of it (a typo in ``require("...")`` is an authorization bug). The set is
    passed in by the caller because ``dc_core`` must not import ``dc_auth``.
    """
    allowed_non_api = allowed_non_api | ({app.openapi_url} if app.openapi_url else frozenset())
    violations: list[tuple[str, str]] = []
    walked = 0

    for rc in iter_route_contexts(app.routes):
        path = rc.path or ""
        if not isinstance(rc.original_route, APIRoute):
            if path not in allowed_non_api:
                violations.append((path, f"unexpected non-API route {path}"))
            continue
        walked += 1
        methods = sorted(rc.methods or ())
        label = f"[{','.join(methods)}] {path}"

        meta = find_policy(rc.dependant)
        if meta is None:
            violations.append((path, f"{label} has no policy"))

        prefix_ok = path.startswith((f"/api/v1/{module}/", "/internal/")) or path in allowed_non_api
        if not prefix_ok:
            violations.append(
                (path, f"{label} is outside /api/v1/{module}/, /internal/ and the allow-list")
            )
        if path.startswith("/api/v1/") and "/internal" in path:
            violations.append((path, f"{label} exposes an /internal path under /api/v1/"))
        if (
            meta is not None
            and not meta.public
            and known_permissions is not None
            and meta.permission not in known_permissions
        ):
            violations.append((path, f"{label} declares unknown permission '{meta.permission}'"))
        if meta is not None and meta.public:
            for method in methods:
                if (method, path) not in allowed_public:
                    violations.append((path, f"{label} is public but not in allowed_public"))
                    break

    if walked < min_routes:
        violations.append(("", f"walked {walked} API routes, expected at least {min_routes}"))

    if violations:
        lines = [msg for _, msg in sorted(violations, key=lambda v: v[0])]
        raise AssertionError("route policy violations:\n" + "\n".join(lines))
