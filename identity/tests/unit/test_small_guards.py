"""Small fail-closed guards: the denial recorder's outcome check, the admin's sign-in method and
the migration command line, each answered before any database or network work."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from dc_auth.authz_wire import CheckContext
from dc_auth.principal import Principal
from dc_identity import migrate
from dc_identity.denials import DenialRecorder
from dc_identity.security_policy import actor_method

pytestmark = pytest.mark.unit

TENANT = "4f1c2d3e-5a6b-4c7d-8e9f-0a1b2c3d4e5f"


def _no_session() -> Any:
    raise AssertionError("an invalid outcome must be refused before a session is opened")


@pytest.mark.parametrize("outcome", ["allow", "", "NOT_FOUND"])
async def test_the_recorder_refuses_an_outcome_that_is_not_a_denial(
    make_principal: Callable[..., Principal], outcome: str
) -> None:
    recorder = DenialRecorder(_no_session)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a denial outcome"):
        await recorder.record(
            make_principal(f"local:{uuid.uuid4()}"),
            "folder:read",
            outcome,  # type: ignore[arg-type]
            folder_id=None,
            scope_path=None,
            context=CheckContext(service="identity", route="/x"),
        )


@pytest.mark.parametrize(
    ("principal_id", "fields", "expected"),
    [
        (f"local:{uuid.uuid4()}", {}, "local"),
        (f"entra:{TENANT}:{uuid.uuid4()}", {}, "microsoft"),
        (f"entra:{TENANT}:{uuid.uuid4()}", {"type": "service_principal"}, None),
        (f"sa:{uuid.uuid4()}", {"type": "service_account"}, None),
        (f"local:{uuid.uuid4()}", {"type": "pat", "via": "pat"}, None),
        ("fake:someone", {}, None),
    ],
)
def test_actor_method_names_only_an_interactive_human_sign_in(
    make_principal: Callable[..., Principal],
    principal_id: str,
    fields: dict[str, Any],
    expected: str | None,
) -> None:
    assert actor_method(make_principal(principal_id, **fields)) == expected


def test_the_migration_command_refuses_a_downgrade_without_a_revision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["migrate", "downgrade"])
    with pytest.raises(SystemExit) as caught:
        migrate.main()
    assert caught.value.code == 2
    assert "downgrade requires a revision" in capsys.readouterr().err
