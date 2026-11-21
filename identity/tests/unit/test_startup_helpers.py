"""Start-up helpers: the bootstrap secret reader, the notifier renewal loop, two lifespan hooks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import structlog
from dc_identity import bootstrap, main
from dc_identity.keys import RECOMMENDED_KEY_BITS
from dc_identity.settings import IdentitySettings

pytestmark = pytest.mark.unit


def test_a_secret_path_that_is_a_directory_is_unreadable(tmp_path: Path) -> None:
    assert bootstrap.read_bootstrap_secret(tmp_path) == (None, "unreadable")


def test_a_secret_file_that_is_not_utf8_is_unreadable(tmp_path: Path) -> None:
    secret_file = tmp_path / "bootstrap_admin_secret"
    secret_file.write_bytes(b"\xff\xfe" + b"x" * 40)
    assert bootstrap.read_bootstrap_secret(secret_file) == (None, "unreadable")


async def test_a_failed_renewal_is_logged_by_class_and_retried_until_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[BaseException] = [ConnectionError("db down"), asyncio.CancelledError()]

    async def flaky(_factory: Any, _settings: Any) -> None:
        raise outcomes.pop(0)

    monkeypatch.setattr(bootstrap, "ensure_notifier", flaky)
    settings = IdentitySettings(notifier_renewal_interval_s=0.001)
    with structlog.testing.capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await bootstrap._renew_notifier_periodically(None, settings)  # type: ignore[arg-type]
    assert outcomes == []
    assert logs == [
        {"event": "notifier_renewal_failed", "error": "ConnectionError", "log_level": "warning"}
    ]


class _Graph:
    closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_the_graph_lifespan_closes_the_graph_client_on_shutdown() -> None:
    graph = _Graph()
    async with main._graph_lifespan(graph)(None):  # type: ignore[arg-type]
        assert graph.closed is False
    assert graph.closed is True


async def test_the_graph_lifespan_without_a_graph_client_yields_nothing_and_closes_nothing() -> (
    None
):
    async with main._graph_lifespan(None)(None) as yielded:  # type: ignore[arg-type]
        assert yielded is None


@pytest.mark.parametrize(
    ("bits", "warned"), [(2048, True), (RECOMMENDED_KEY_BITS, False), (4096, False)]
)
async def test_only_a_key_below_the_recommended_size_is_warned_about(
    bits: int, warned: bool
) -> None:
    with structlog.testing.capture_logs() as logs:
        async with main._key_size_hook(bits)(None):  # type: ignore[arg-type]
            pass
    events = [entry["event"] for entry in logs]
    assert events == (["signing_key_below_recommended_size"] if warned else [])
