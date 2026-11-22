"""Root pytest configuration: shared plugins for every workspace member.

It also holds the backend operation tracer used by the operation-coverage gate
(``scripts/operation_coverage.py``, run by ``CI_FULL=1 bash scripts/ci.sh``). When the environment
variable ``DC_OP_TRACE`` names a file, every request a test sends through ``httpx.ASGITransport``
(the only in-process transport the suites use, see ``dc_core.testing.asgi_client``) is recorded as
one JSON line: method, path (no query string), status and the test node id. Never a header, token
or body. With the variable unset the tracer is not installed and nothing changes.

And it keeps the session's heap small: after every test, library caches let go of the apps and
engines that test built (``_release_library_caches``), and every ``timing`` test starts with the
earlier tests' garbage collected and frozen out of its measurement (``_timing_test_gc_isolation``).
"""

from __future__ import annotations

import gc
import json
import os
from collections.abc import Iterator
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


@pytest.fixture(autouse=True)
def _release_library_caches() -> Iterator[None]:
    """After every test, library caches let go of the apps and engines the test built.

    Without this, FastAPI's and SQLAlchemy's process-wide caches kept about 14,000 objects of every
    test that starts the services (``dc_core.testing.release_library_caches``). Halfway through the
    CI integration run the pytest process then held 3.7 million objects, and a full garbage
    collection of them took up to 3.7 s on a GitHub runner, longer than contracts' 3 s deadline for
    a call to validation: a rule-set dry run answered 503 ``validation-unavailable`` where its test
    expected 200.
    """
    # Imported here, not at the top: ``pytest_plugins`` must import dc_core.testing first, so that
    # pytest can rewrite its asserts.
    from dc_core.testing import release_library_caches

    yield
    release_library_caches()


@pytest.fixture(autouse=True)
def _timing_test_gc_isolation(request: pytest.FixtureRequest) -> Iterator[None]:
    """A ``timing`` test measures the code under test, not the garbage earlier tests left.

    One pytest process runs a whole suite. ``_release_library_caches`` keeps the earlier tests'
    apps and engines from staying reachable, but they are reference cycles that only a garbage
    collection frees: an allocation-heavy test could still trigger a full collection of them
    inside its measured window and count it against the code under test. The validation service
    keeps no objects per run (none left after 330 runs in one app), so a deployed process has no
    such heap.

    So before a ``timing`` test the garbage is collected and the surviving objects are frozen
    (``gc.freeze``: moved to the permanent generation, which collections skip); they are unfrozen
    afterwards. Autouse fixtures are set up before the test's own fixtures, so everything the test
    builds (the apps, the sandbox, the documents) is collected as usual and the services' own GC
    cost stays inside every measurement.
    """
    if request.node.get_closest_marker("timing") is None:
        yield
        return
    gc.collect()
    gc.freeze()
    try:
        yield
    finally:
        gc.unfreeze()
