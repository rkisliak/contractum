"""The api-docs server's own hardening, checked without a stack (IN-01, IN-03).

``gateway/api-docs/server.py`` is loaded by path, as ``test_openapi_merge.py`` loads ``merge.py``;
loading it starts nothing (the server and the merge thread start only in ``main()``).
"""

from __future__ import annotations

import importlib.util
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
