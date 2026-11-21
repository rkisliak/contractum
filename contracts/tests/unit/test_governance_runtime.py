"""Governance settings at runtime and the start-up checks of ``ContractsSettings`` (0ye).

``effective_governance`` never raises: a section never read or one that does not parse falls back
to the env values. ``ContractsSettings`` refuses a malformed webhook key, notifier key or default
ODCS version at start-up, and never echoes a secret in the error.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from dc_contracts.runtime_settings import effective_governance
from dc_contracts.settings import ContractsSettings
from dc_core.platform_settings import RuntimeSettingsClient
from pydantic import ValidationError

pytestmark = pytest.mark.unit

BASE = "http://identity:8000"
URL = f"{BASE}/internal/v1/settings/runtime"
CALLER = "caller-token"  # noqa: S105 - a test token, not a secret


def serve_governance(stored: object) -> respx.Route:
    return respx.get(URL).mock(
        return_value=httpx.Response(200, json={"sections": {"governance": stored}})
    )


@respx.mock
async def test_a_governance_section_never_read_means_the_env_values() -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    env = ContractsSettings(require_lifecycle_reason=True)

    effective = await effective_governance(RuntimeSettingsClient(BASE), CALLER, env)

    assert effective.default_odcs_api_version == env.default_odcs_api_version
    assert effective.require_lifecycle_reason is True


@respx.mock
async def test_a_stored_governance_section_that_does_not_parse_means_the_env_values() -> None:
    serve_governance({"mandatory_warn_days": "many", "require_lifecycle_reason": True})
    env = ContractsSettings()

    effective = await effective_governance(RuntimeSettingsClient(BASE), CALLER, env)

    assert effective.require_lifecycle_reason is env.require_lifecycle_reason
    assert effective.default_odcs_api_version == env.default_odcs_api_version


@respx.mock
async def test_without_a_client_or_bearer_governance_is_the_env_and_nothing_is_read() -> None:
    route = serve_governance({"require_lifecycle_reason": True})
    env = ContractsSettings()

    for client, bearer in ((None, CALLER), (RuntimeSettingsClient(BASE), None)):
        effective = await effective_governance(client, bearer, env)
        assert effective.require_lifecycle_reason is env.require_lifecycle_reason

    assert not route.called


@respx.mock
async def test_a_stored_reason_requirement_overrides_the_env_one() -> None:
    route = serve_governance({"require_lifecycle_reason": True})

    effective = await effective_governance(RuntimeSettingsClient(BASE), CALLER, ContractsSettings())

    assert effective.require_lifecycle_reason is True
    assert route.calls.last.request.headers["authorization"] == f"Bearer {CALLER}"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("webhook_secret_key", "definitely-not-fernet", "not a valid Fernet key"),
        ("mail_api_key", "two words", "mail_api_key is not a valid key"),
    ],
)
def test_a_malformed_secret_is_refused_at_start_up_without_echoing_it(
    field: str, value: str, message: str
) -> None:
    with pytest.raises(ValidationError) as exc:
        ContractsSettings(**{field: value})  # type: ignore[arg-type]

    assert message in str(exc.value)
    assert value not in str(exc.value)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("mail_api_key", "", "mail_api_key is not a valid key"),
        ("default_odcs_api_version", "v9.9.9", "default_odcs_api_version must be one of"),
    ],
)
def test_an_empty_key_or_unsupported_default_version_is_refused(
    field: str, value: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ContractsSettings(**{field: value})  # type: ignore[arg-type]
