"""The test session's heap: a closed stack leaves no engine or dialect behind.

Every stack test builds its own identity, validation and contracts apps, each with its own engine
and dialect. FastAPI's dependency classification caches and SQLAlchemy's mapper and dialect caches
are process-wide, so they kept every test's engines and dialects, with their compiled statements,
alive after the test: about 14,000 objects per test. Halfway through the CI integration run the
pytest process held 3.7 million objects, and a full garbage collection of them took up to 3.7 s on
a GitHub runner, longer than contracts' 3 s deadline for a call to validation (a dry run answered
503).

The root ``conftest.py`` releases those caches after every test
(``dc_core.testing.release_library_caches``). This test fails as soon as a library update moves a
cache out of its reach, or a new cache starts keeping a test's engine.
"""

from __future__ import annotations

import asyncio
import gc
import uuid
import weakref
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from dc_core.testing import release_library_caches
from sqlalchemy.engine import Dialect

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]

DOC = (
    b'apiVersion: v3.2.0\nkind: DataContract\nid: urn:dc:heap:orders\nversion: "1.0.0"\n'
    b"status: draft\nname: Orders\nschema:\n  - name: orders\n    properties:\n"
    b"      - name: id\n        logicalType: string\n"
)


def _live_dialects() -> list[Dialect]:
    return [obj for obj in gc.get_objects() if isinstance(obj, Dialect)]


async def test_a_closed_stack_keeps_no_engine_or_dialect_alive(make_stack: MakeStack) -> None:
    earlier = weakref.WeakSet(_live_dialects())
    async with make_stack() as stack:
        # Writes in all three services: the admin login, a folder chain, a contract published
        # through a validation run.
        org = await stack.folder(stack.root_id, f"o{uuid.uuid4().hex[:10]}")
        team = await stack.team_in_org(org, "one")
        contract, draft, etag = await stack.create_contract(team, DOC)
        assert (await stack.publish(contract, draft, etag)).status_code == 201
        apps = (stack.identity_app, stack.validation_app, stack.contracts_app)
        engines = [weakref.ref(app.state.session_factory.kw["bind"]) for app in apps]
        del apps
    del stack
    # The services' cancelled background tasks finish with callbacks still queued on the event
    # loop, and those hold the tasks (and through them a session factory) until they run.
    await asyncio.sleep(0)

    release_library_caches()
    gc.collect()

    assert [engine() for engine in engines] == [None, None, None]
    assert [dialect for dialect in _live_dialects() if dialect not in earlier] == []
