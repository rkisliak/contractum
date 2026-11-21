"""Shared builders for identity's unit tests (no database, no Docker)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from dc_auth.principal import Principal


@pytest.fixture
def make_principal() -> Callable[..., Principal]:
    """A verified caller with the given id; every other field has a harmless default."""

    def _make(principal_id: str, **fields: Any) -> Principal:
        values: dict[str, Any] = {
            "type": "user",
            "display": "Test Caller",
            "issuer": "https://issuer.test",
            "subject": principal_id.rsplit(":", 1)[-1],
            "via": "jwt",
            "scopes": frozenset({"api"}),
        }
        values.update(fields)
        return Principal(id=principal_id, **values)

    return _make
