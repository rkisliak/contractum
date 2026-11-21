"""The sign-in User-Agent is reduced to a fixed-vocabulary summary; the raw string is never kept."""

from __future__ import annotations

import pytest
from dc_identity.sessions import CLIENT_SUMMARY_MAX, summarize_user_agent

pytestmark = pytest.mark.unit

CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
EDGE_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"
SAFARI_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (CHROME_MAC, "Chrome on macOS"),
        (EDGE_WIN, "Edge on Windows"),
        (FIREFOX_LINUX, "Firefox on Linux"),
        (SAFARI_IOS, "Safari on iOS"),
        (CHROME_ANDROID, "Chrome on Android"),
        ("curl/8.5.0", "API client (curl)"),
        ("python-httpx/0.28.1", "API client (python-httpx)"),
        ("python-requests/2.32.3", "API client (python-requests)"),
        ("Go-http-client/2.0", "API client (Go-http-client)"),
        ("something entirely unknown", "Other client"),
    ],
)
def test_known_clients_get_their_fixed_summary(raw: str, expected: str) -> None:
    assert summarize_user_agent(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_a_missing_or_blank_user_agent_has_no_summary(raw: str | None) -> None:
    assert summarize_user_agent(raw) is None


def test_the_output_never_carries_input_and_stays_short() -> None:
    marker = "PLANTED-MARKER-9f3a"
    for raw in (
        f"{marker} curl/8.5",
        f"Mozilla/5.0 ({marker}; Linux) Chrome/1 {marker}",
        marker * 100,
        f"{marker}/1.0 Edg/2",
    ):
        out = summarize_user_agent(raw)
        assert out is not None
        assert marker not in out
        assert len(out) <= CLIENT_SUMMARY_MAX == 64
