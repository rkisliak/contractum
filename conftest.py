"""Root pytest configuration: shared plugins for every workspace member.

It also holds the backend operation tracer used by the operation-coverage gate
(``scripts/operation_coverage.py``, run by ``CI_FULL=1 bash scripts/ci.sh``). When the environment
variable ``DC_OP_TRACE`` names a file, every request a test sends through ``httpx.ASGITransport``
(the only in-process transport the suites use, see ``dc_core.testing.asgi_client``) is recorded as
one JSON line: method, path (no query string), status and the test node id. Never a header, token
or body. With the variable unset the tracer is not installed and nothing changes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

pytest_plugins = ("dc_core.testing", "dc_auth.testing")

_TRACE_ENV = "DC_OP_TRACE"
_current_test: list[str] = [""]


def _install_tracer(trace_file: Path) -> None:
    import httpx

    original = httpx.ASGITransport.handle_async_request
    if getattr(original, "_dc_traced", False):
        return
    handle = trace_file.open("a", encoding="utf-8")

    async def traced(self: httpx.ASGITransport, request: httpx.Request) -> httpx.Response:
        response = await original(self, request)
        record: dict[str, Any] = {
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "test": _current_test[0],
        }
        handle.write(json.dumps(record) + "\n")
        handle.flush()
        return response

    traced._dc_traced = True  # type: ignore[attr-defined]
    httpx.ASGITransport.handle_async_request = traced  # type: ignore[method-assign]


def pytest_configure(config: pytest.Config) -> None:
    target = os.environ.get(_TRACE_ENV)
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        _install_tracer(path)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    _current_test[0] = item.nodeid
