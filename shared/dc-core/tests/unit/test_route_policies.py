"""PolicyRouter refusal and the route walker (PLAT-04, T-01-03)."""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from dc_core.app import create_app
from dc_core.policy import (
    PolicyMeta,
    PolicyRouter,
    assert_all_routes_have_policy,
    mark_policy,
    public,
)
from dc_core.settings import CoreSettings
from fastapi import APIRouter, Depends, FastAPI, Query

pytestmark = pytest.mark.unit

NON_API = frozenset({"/healthz", "/readyz"})
PUBLIC = frozenset({("GET", "/healthz"), ("GET", "/readyz")})


def guard(permission: str = "probe:read") -> Any:
    async def _policy() -> None:
        return None

    mark_policy(_policy, PolicyMeta(permission))
    return Depends(_policy)


async def unguarded() -> dict[str, str]:
    return {}


async def guarded_in_signature(_: Annotated[None, guard()]) -> dict[str, str]:
    return {}


async def guarded_by_default(_: None = guard()) -> dict[str, str]:  # noqa: B008
    return {}


def _app(*routers: APIRouter) -> FastAPI:
    return create_app(
        service="probe",
        settings=CoreSettings(service="probe", database_url="postgresql+psycopg://x/y"),
        routers=list(routers),
    )


def _walk(app: FastAPI, **kwargs: Any) -> None:
    assert_all_routes_have_policy(
        app, module="probe", allowed_non_api=NON_API, allowed_public=PUBLIC, **kwargs
    )


def test_policy_router_refuses_route_without_policy() -> None:
    router = PolicyRouter()
    with pytest.raises(
        RuntimeError, match="PolicyRouter refused: route /api/v1/probe/bad has no policy"
    ):
        router.add_api_route("/api/v1/probe/bad", unguarded)


def test_policy_router_refuses_decorated_route_without_policy() -> None:
    router = PolicyRouter()
    with pytest.raises(RuntimeError, match="PolicyRouter refused"):
        router.get("/api/v1/probe/bad")(unguarded)


def test_policy_router_refuses_empty_dependency_list() -> None:
    router = PolicyRouter()
    with pytest.raises(RuntimeError, match="PolicyRouter refused"):
        router.add_api_route("/api/v1/probe/bad", unguarded, dependencies=[])


def test_policy_router_ordering_accepts_policy_wherever_declared() -> None:
    router = PolicyRouter()
    router.add_api_route("/api/v1/probe/a", unguarded, dependencies=[guard()])
    router.add_api_route("/api/v1/probe/b", guarded_in_signature)
    router.add_api_route("/api/v1/probe/c", guarded_by_default)
    router.add_api_route("/api/v1/probe/d", unguarded, dependencies=[public()])
    guarded_router = PolicyRouter(dependencies=[guard()])
    guarded_router.add_api_route("/api/v1/probe/e", unguarded)
    assert len(router.routes) == 4
    assert len(guarded_router.routes) == 1


def test_walker_unguarded_route_added_to_app_is_named() -> None:
    app = _app()
    app.add_api_route("/api/v1/probe/unguarded", unguarded, methods=["POST"])
    with pytest.raises(AssertionError, match=r"\[POST\] /api/v1/probe/unguarded has no policy"):
        _walk(app)


def test_walker_adjacency_prefix_rule() -> None:
    app = _app()
    for path in ("/api/v1/probe-evil/x", "/internalx/y", "/api/v1/probe/internal/z"):
        app.add_api_route(path, unguarded, methods=["GET"], dependencies=[guard()])
    with pytest.raises(AssertionError) as info:
        _walk(app)
    text = str(info.value)
    assert "/api/v1/probe-evil/x" in text
    assert "/internalx/y" in text
    assert "/api/v1/probe/internal/z" in text


def test_walker_empty_app_fails_min_routes() -> None:
    bare = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    with pytest.raises(AssertionError, match="walked 0 API routes"):
        _walk(bare)


def test_walker_empty_dependency_list_has_no_policy() -> None:
    app = _app()
    app.add_api_route("/api/v1/probe/empty", unguarded, methods=["GET"], dependencies=[])
    with pytest.raises(AssertionError, match=r"\[GET\] /api/v1/probe/empty has no policy"):
        _walk(app)


def test_walker_ordering_finds_policy_everywhere_and_reports_sorted() -> None:
    plain = APIRouter()
    plain.add_api_route("/api/v1/probe/via-include", unguarded, methods=["GET"])
    app = _app()
    app.include_router(plain, dependencies=[guard()])
    app.add_api_route("/api/v1/probe/on-route", unguarded, methods=["GET"], dependencies=[guard()])
    app.add_api_route("/api/v1/probe/in-signature", guarded_in_signature, methods=["GET"])
    _walk(app)  # no violation: all three declare a policy somewhere

    app.add_api_route("/api/v1/probe/zzz", unguarded, methods=["GET"])
    app.add_api_route("/api/v1/probe/aaa", unguarded, methods=["POST"])
    with pytest.raises(AssertionError) as info:
        _walk(app)
    text = str(info.value)
    assert text.index("/api/v1/probe/aaa") < text.index("/api/v1/probe/zzz")


def test_walker_requires_public_routes_to_be_allow_listed() -> None:
    app = _app()
    app.add_api_route("/api/v1/probe/open", unguarded, methods=["GET"], dependencies=[public()])
    with pytest.raises(AssertionError, match="public but not in allowed_public"):
        _walk(app)
    assert_all_routes_have_policy(
        app,
        module="probe",
        allowed_non_api=NON_API,
        allowed_public=PUBLIC | {("GET", "/api/v1/probe/open")},
    )


def test_walker_flags_unexpected_non_api_route() -> None:
    app = _app()
    app.add_api_route("/api/v1/probe/ok", unguarded, methods=["GET"], dependencies=[guard()])
    app.add_route("/sneaky", lambda request: None)  # a plain Starlette route
    with pytest.raises(AssertionError, match="unexpected non-API route /sneaky"):
        _walk(app)


def test_walker_passes_for_a_compliant_app() -> None:
    router = PolicyRouter()
    router.add_api_route(
        "/api/v1/probe/widgets", unguarded, methods=["POST"], dependencies=[guard()]
    )
    internal = PolicyRouter()
    internal.add_api_route(
        "/internal/v1/authz/check", unguarded, methods=["POST"], dependencies=[guard()]
    )
    app = create_app(
        service="probe",
        settings=CoreSettings(service="probe", database_url="postgresql+psycopg://x/y"),
        routers=[router],
        internal_routers=[internal],
    )
    _walk(app, min_routes=3)


def test_walker_rejects_unknown_permission_naming_method_path_and_permission() -> None:
    app = _app()
    app.add_api_route(
        "/api/v1/probe/widgets",
        unguarded,
        methods=["POST"],
        dependencies=[guard("widget:frobnicate")],
    )
    with pytest.raises(AssertionError) as info:
        _walk(app, known_permissions=frozenset({"widget:read"}))
    assert "[POST] /api/v1/probe/widgets declares unknown permission 'widget:frobnicate'" in str(
        info.value
    )


def test_walker_without_known_permissions_stays_backwards_compatible() -> None:
    app = _app()
    app.add_api_route(
        "/api/v1/probe/widgets",
        unguarded,
        methods=["POST"],
        dependencies=[guard("widget:frobnicate")],
    )
    _walk(app)
    _walk(app, known_permissions=None)
    _walk(app, known_permissions=frozenset({"widget:frobnicate"}))


def test_walker_never_checks_public_routes_against_known_permissions() -> None:
    app = _app()
    app.add_api_route("/api/v1/probe/open", unguarded, methods=["GET"], dependencies=[public()])
    assert_all_routes_have_policy(
        app,
        module="probe",
        allowed_non_api=NON_API,
        allowed_public=PUBLIC | {("GET", "/api/v1/probe/open")},
        known_permissions=frozenset({"widget:read"}),
    )


async def nested_guard(_: Annotated[None, guard("probe:nested")]) -> None:
    return None


async def plain_helper() -> None:
    return None


def test_a_policy_behind_a_nested_dependency_is_found_by_router_and_walker() -> None:
    router = PolicyRouter(prefix="/api/v1/probe")

    async def endpoint(
        q: Annotated[str, Query(max_length=5)], _: Annotated[None, Depends(nested_guard)]
    ) -> dict[str, str]:
        return {"q": q}

    router.add_api_route("/nested", endpoint, methods=["GET"])
    app = _app(router)
    _walk(app, min_routes=1, known_permissions=frozenset({"probe:nested"}))


def test_a_repeated_or_bare_dependency_without_policy_is_refused() -> None:
    router = PolicyRouter()

    async def endpoint(
        a: Annotated[None, Depends(plain_helper)],
        b: Annotated[None, Depends(plain_helper)],
        c: Annotated[CoreSettings, Depends()],
    ) -> dict[str, str]:
        return {}

    with pytest.raises(RuntimeError, match="PolicyRouter refused"):
        router.add_api_route("/api/v1/probe/repeated", endpoint)


def test_unresolvable_annotations_fall_back_to_the_plain_signature() -> None:
    async def endpoint(x, _=guard()):  # type: ignore[no-untyped-def]  # noqa: B008
        return {}

    endpoint.__annotations__ = {"x": "NameThatDoesNotExist"}
    router = PolicyRouter()
    router.add_api_route("/api/v1/probe/forward", endpoint)
    assert len(router.routes) == 1


def test_a_dependency_without_a_signature_carries_no_policy() -> None:
    router = PolicyRouter()
    with pytest.raises(RuntimeError, match="PolicyRouter refused"):
        router.add_api_route("/api/v1/probe/builtin", unguarded, dependencies=[Depends(min)])


def test_the_walker_looks_past_a_dependency_without_policy() -> None:
    router = PolicyRouter(prefix="/api/v1/probe")
    router.add_api_route(
        "/after-helper",
        unguarded,
        methods=["GET"],
        dependencies=[Depends(plain_helper), guard("probe:after")],
    )
    _walk(_app(router), known_permissions=frozenset({"probe:after"}))
