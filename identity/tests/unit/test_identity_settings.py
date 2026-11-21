"""``IdentitySettings`` refuses out-of-range values at start-up with one message per setting."""

from __future__ import annotations

import pytest
from dc_identity.settings import IdentitySettings
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def test_the_defaults_are_valid() -> None:
    settings = IdentitySettings()
    assert settings.max_active_tokens == 10
    assert settings.session_max_s == 604800


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"graph_token_url_template": "https://login.example/token"}, "{tenant_id}"),
        ({"directory_cache_ttl_s": -1}, "directory_cache_ttl_s must be >= 0"),
        ({"graph_max_pages": 0}, "graph_max_pages and directory_cache_size must be >= 1"),
        ({"directory_cache_size": 0}, "graph_max_pages and directory_cache_size must be >= 1"),
        ({"session_max_s": 3599}, "session_max_s must be >= 3600"),
        ({"credential_max_ttl_days": 0}, "credential_max_ttl_days must be >= 1"),
        ({"max_active_tokens": 0}, "max_active_tokens must be between 1 and 1000"),
        ({"max_active_tokens": 1001}, "max_active_tokens must be between 1 and 1000"),
        ({"max_sessions_per_user": 101}, "max_sessions_per_user must be between 1 and 100"),
        ({"credential_expiry_warning_days": -1}, "credential_expiry_warning_days must be >= 0"),
        ({"credential_usage_flush_s": 0}, "credential_usage_flush_s must be > 0"),
        ({"notifier_key_lifetime_days": 0}, "notifier_key_lifetime_days must be >= 1"),
        ({"notifier_renewal_interval_s": 0}, "notifier_renewal_interval_s must be > 0"),
        ({"notifier_account_name": "   "}, "notifier_account_name must be 1 to 100 characters"),
        ({"notifier_account_name": "n" * 101}, "notifier_account_name must be 1 to 100 characters"),
        (
            {"principal_resolve_names_per_minute": 0},
            "principal_resolve_names_per_minute must be >= 1",
        ),
    ],
)
def test_an_out_of_range_setting_is_refused_with_its_own_message(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        IdentitySettings(**overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_max_s": 3600},
        {"max_active_tokens": 1000},
        {"notifier_account_name": "n" * 100},
        {"credential_expiry_warning_days": 0},
    ],
)
def test_the_boundary_values_are_accepted(overrides: dict[str, object]) -> None:
    settings = IdentitySettings(**overrides)  # type: ignore[arg-type]
    for name, value in overrides.items():
        assert getattr(settings, name) == value
