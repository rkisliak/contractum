"""The settings store's pure parts: defaults of a section without identity's view, and loading
rows that include a section this version no longer knows."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from dc_core.platform_settings import SECTIONS
from dc_identity.platform_settings import load_all, section_defaults

pytestmark = pytest.mark.unit


def test_security_defaults_without_identity_s_env_view_are_all_unset() -> None:
    view = section_defaults("security", entra_tenant_ids=())
    assert view == dict.fromkeys(SECTIONS["security"].model_fields)


def test_governance_defaults_merge_only_the_section_s_own_keys() -> None:
    view = section_defaults(
        "governance",
        entra_tenant_ids=(),
        service_defaults={"validation": {"require_lifecycle_reason": True, "unrelated": 1}},
    )
    assert view["require_lifecycle_reason"] is True
    assert "unrelated" not in view


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _Session:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, _statement: Any) -> _Rows:
        return _Rows(self._rows)


async def test_load_all_keeps_known_sections_and_skips_a_retired_one() -> None:
    at = datetime(2026, 11, 1, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            section="security",
            value={"max_active_tokens": 5},
            revision=3,
            updated_at=at,
            updated_by="local:a",
        ),
        SimpleNamespace(
            section="retired", value={"x": 1}, revision=1, updated_at=at, updated_by="b"
        ),
    ]
    stored = await load_all(_Session(rows))  # type: ignore[arg-type]
    assert set(stored) == set(SECTIONS)
    assert stored["security"].value == {"max_active_tokens": 5}
    assert stored["security"].revision == 3
    assert stored["entra"].revision == 0
    assert stored["entra"].value == {}
