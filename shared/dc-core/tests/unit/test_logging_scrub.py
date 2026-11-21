"""Secrets never reach logs (T-01-06)."""

import io
import json

import pytest
import structlog
from dc_core.logging import configure_logging, scrub_secrets

pytestmark = pytest.mark.unit


def test_log_call_redacts_password_and_authorization() -> None:
    stream = io.StringIO()
    configure_logging("probe", "INFO", stream=stream)
    structlog.get_logger("t").info("login", password="x", authorization="Bearer y", user="alice")
    record = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert record["password"] == "[redacted]"
    assert record["authorization"] == "[redacted]"
    assert record["user"] == "alice"
    assert record["service"] == "probe"
    assert "Bearer y" not in stream.getvalue()


def test_scrub_secrets_handles_nested_keys_and_key_variants() -> None:
    scrubbed = scrub_secrets(
        None,
        "info",
        {
            "event": "e",
            "refresh_token": "t",
            "client_secret": "s",
            "Set-Cookie": "c",
            "private_key": "k",
            "nested": {"Password": "p", "ok": 1},
            "items": [{"token": "z"}],
        },
    )
    assert scrubbed["refresh_token"] == "[redacted]"
    assert scrubbed["client_secret"] == "[redacted]"
    assert scrubbed["Set-Cookie"] == "[redacted]"
    assert scrubbed["private_key"] == "[redacted]"
    assert scrubbed["nested"] == {"Password": "[redacted]", "ok": 1}
    assert scrubbed["items"] == [{"token": "[redacted]"}]
    assert scrubbed["event"] == "e"


def test_exception_logging_never_renders_frame_locals() -> None:
    stream = io.StringIO()
    configure_logging("probe", "INFO", stream=stream)

    def fail() -> None:
        authorization = "Bearer LEAKED-TOKEN"  # noqa: F841 - a local that must not be logged
        raise RuntimeError("boom")

    try:
        fail()
    except RuntimeError as exc:
        structlog.get_logger("t").error("unhandled", exc_info=exc)
    assert "LEAKED-TOKEN" not in stream.getvalue()
    assert "boom" in stream.getvalue()


def test_a_dcp_token_is_redacted_by_value_even_under_a_harmless_key() -> None:
    import dc_core.logging as dc_logging

    secret = "A" * 43
    token = f"dcp_0123456789abcdef_{secret}"
    stream = io.StringIO()
    configure_logging("probe", "INFO", stream=stream)
    structlog.get_logger("t").info(
        f"using {token}", note=f"see {token} here", nested={"note": token, "ok": 1}, items=[token]
    )
    rendered = stream.getvalue()
    record = json.loads(rendered.strip().splitlines()[-1])
    assert getattr(dc_logging, "DCP_TOKEN_RE", None) is not None
    assert secret not in rendered
    assert "0123456789abcdef" not in rendered
    assert "dcp_" + dc_logging.REDACTED in rendered
    assert record["nested"]["ok"] == 1
    assert record["items"] == ["dcp_[redacted]"]


def test_text_that_only_resembles_a_dcp_token_is_left_alone() -> None:
    scrubbed = scrub_secrets(None, "info", {"event": "dcp_short and dcp_zzzz_x", "n": 3})
    assert scrubbed == {"event": "dcp_short and dcp_zzzz_x", "n": 3}


WHSEC_BASE64 = "dGhpcy1pcy1hLTMyLWJ5dGUtd2ViaG9vay1rZXkh+/Zm=="
WHSEC = "whsec_" + WHSEC_BASE64


def test_a_whsec_secret_is_redacted_by_value_even_under_a_harmless_key() -> None:
    import dc_core.logging as dc_logging

    stream = io.StringIO()
    configure_logging("probe", "INFO", stream=stream)
    structlog.get_logger("t").info(
        f"secret is {WHSEC}", note=f"secret is {WHSEC}", detail=[{"line": WHSEC}, WHSEC], ok=1
    )
    rendered = stream.getvalue()
    record = json.loads(rendered.strip().splitlines()[-1])
    assert getattr(dc_logging, "WHSEC_RE", None) is not None
    assert WHSEC_BASE64 not in rendered
    assert WHSEC_BASE64[:20] not in rendered
    assert "whsec_" + dc_logging.REDACTED in rendered
    assert record["ok"] == 1
    assert record["note"] == "secret is whsec_" + dc_logging.REDACTED
    assert record["detail"][0]["line"] == "whsec_" + dc_logging.REDACTED


def test_a_whsec_secret_under_a_credential_looking_key_is_still_redacted() -> None:
    scrubbed = scrub_secrets(None, "info", {"event": "e", "webhook_secret": WHSEC})
    assert scrubbed["webhook_secret"] == "[redacted]"


def test_text_that_only_looks_like_the_prefix_is_left_alone() -> None:
    scrubbed = scrub_secrets(None, "info", {"event": "whsec_ is the prefix; whsec_short stays"})
    assert scrubbed["event"] == "whsec_ is the prefix; whsec_short stays"


def test_dcp_redaction_still_works_next_to_whsec() -> None:
    token = "dcp_0123456789abcdef_" + "B" * 43
    scrubbed = scrub_secrets(None, "info", {"event": f"{token} and {WHSEC}"})
    assert scrubbed["event"] == "dcp_[redacted] and whsec_[redacted]"
