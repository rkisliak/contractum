"""Every mail route is declared with require() or public() (D-07, PLAT-04)."""

from __future__ import annotations

import pytest
from dc_auth.roles import PERMISSIONS
from dc_core.policy import assert_all_routes_have_policy, find_policy
from dc_mail.main import create_mail_app
from dc_mail.settings import MailSettings
from fastapi.routing import APIRoute, iter_route_contexts

pytestmark = pytest.mark.unit

ALLOWED_PUBLIC = frozenset({("GET", "/healthz"), ("GET", "/readyz")})
ALLOWED_NON_API = frozenset({"/healthz", "/readyz"})


def _app():  # type: ignore[no-untyped-def]
    return create_mail_app(MailSettings())


def test_every_mail_route_declares_a_policy_and_only_the_health_routes_are_public() -> None:
    assert_all_routes_have_policy(
        _app(),
        module="mail",
        allowed_non_api=ALLOWED_NON_API,
        allowed_public=ALLOWED_PUBLIC,
        min_routes=4,
        known_permissions=frozenset(PERMISSIONS),
    )


def test_send_route_requires_mail_send_and_is_the_only_non_public_route() -> None:
    non_public: dict[tuple[str, str], str] = {}
    public: set[tuple[str, str]] = set()
    for rc in iter_route_contexts(_app().routes):
        if not isinstance(rc.original_route, APIRoute):
            continue
        meta = find_policy(rc.dependant)
        assert meta is not None, rc.path
        for method in rc.methods or ():
            key = (method, rc.path or "")
            if meta.public:
                public.add(key)
            else:
                non_public[key] = meta.permission or ""
    assert public == ALLOWED_PUBLIC
    assert non_public == {
        ("POST", "/internal/v1/mail/send"): "mail:send",
        ("GET", "/internal/v1/about"): "settings:read",
    }


def test_openapi_is_served_under_the_module_prefix_and_lists_no_internal_path() -> None:
    app = _app()
    assert app.openapi_url == "/api/v1/mail/openapi.json"
    assert not [path for path in app.openapi()["paths"] if "/internal" in path]
