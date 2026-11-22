"""The api-docs server's own hardening, checked without a stack (IN-01, IN-03).

``gateway/api-docs/server.py`` is loaded by path, as ``test_openapi_merge.py`` loads ``merge.py``;
loading it starts nothing (the server and the merge thread start only in ``main()``).
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

SERVER = Path(__file__).resolve().parents[2] / "gateway" / "api-docs" / "server.py"


@pytest.fixture(scope="module")
def server() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gateway_api_docs_server", SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_swagger_ui_may_not_be_framed(server: ModuleType) -> None:
    headers = dict(server.SECURITY_HEADERS)
    directives = [d.strip() for d in headers["Content-Security-Policy"].split(";")]
    assert "frame-ancestors 'none'" in directives
    assert "default-src 'self'" in directives


def test_a_silent_connection_cannot_hold_a_handler_thread_forever(server: ModuleType) -> None:
    # BaseHTTPRequestHandler applies ``timeout`` to the connection socket (settimeout).
    assert server.Handler.timeout == 30


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {
                "DC_VERSION": "1.4.0",
                "DC_GIT_SHA": "abc1234def56",
                "DC_BUILD_TIME": "2026-11-21T07:00:00Z",
            },
            ("1.4.0", "abc1234def56", "2026-11-21T07:00:00Z"),
        ),
        ({"DC_VERSION": "", "DC_GIT_SHA": "", "DC_BUILD_TIME": ""}, ("dev", "dev", "dev")),
    ],
    ids=["release-build", "local-build"],
)
def test_the_version_route_answers_the_build_like_the_services(
    server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    expected: tuple[str, str, str],
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/docs/version"
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local test server
            body = json.loads(response.read())
            headers = response.headers
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert body == dict(
        zip(("service", "version", "commit", "built_at"), ("api-docs", *expected), strict=True)
    )
    assert headers["Cache-Control"] == "public, max-age=60"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Content-Type-Options"] == "nosniff"
