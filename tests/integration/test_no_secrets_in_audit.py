"""AUD-03 / T-01-53: secrets never reach the outbox, the audit store, the logs or an error body.

The scan list is the operational definition of "secret" (Plan 12 flagged assumption): Argon2 hashes,
the bootstrap secret, JWTs, raw refresh tokens and their SHA-256 hashes, personal access
tokens (raw, secret part, SHA-256 and the stored hash and public id), the new password, a private
key and a secret-looking ``customProperties`` value. Failures name the stores and labels only,
never the matched secret itself. The scan list is never to be weakened: a hit is fixed at its
source (allow-list, scrubber, problem detail).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _needles(flow: Any, sql: Any) -> dict[str, str]:
    needles = {
        "argon2 hash prefix": "$argon2",
        "JWT prefix": "eyJ",
        "private key": "PRIVATE KEY",
        **flow.secrets_to_hide(),
    }
    for index, raw in enumerate(flow.refresh_tokens):
        needles[f"refresh token #{index} sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    for index, row in enumerate(sql(flow.databases.identity, "SELECT password_hash FROM users")):
        if row["password_hash"]:
            needles[f"stored password hash #{index}"] = row["password_hash"]
    for index, row in enumerate(
        sql(flow.databases.identity, "SELECT token_hash FROM refresh_tokens")
    ):
        needles[f"stored refresh-token hash #{index}"] = row["token_hash"]
    for index, row in enumerate(
        sql(flow.databases.identity, "SELECT public_id, secret_hash FROM api_credentials")
    ):
        needles[f"stored PAT secret hash #{index}"] = row["secret_hash"]
        needles[f"stored PAT public id #{index}"] = row["public_id"]
    return needles


def _hits(haystacks: dict[str, str], needles: dict[str, str]) -> list[str]:
    return [
        f"{store} contains {label}"
        for store, text in haystacks.items()
        for label, needle in needles.items()
        if needle in text
    ]


def test_the_scanner_reports_a_planted_secret() -> None:
    hits = _hits({"store": "x $argon2id$v=19 y", "clean": "nothing"}, {"hash": "$argon2"})
    assert hits == ["store contains hash"]


async def test_scan_inputs_are_not_vacuous(
    completed_flow: Any, captured_logs: Any, sql: Any, outbox_rows: Callable[[Any], Any]
) -> None:
    """Every scanned store is populated, and the secrets really exist at their source."""
    flow = completed_flow
    users = sql(flow.databases.identity, "SELECT password_hash FROM users WHERE username = 'admin'")
    assert str(users[0]["password_hash"]).startswith("$argon2id$")
    stored = {
        r["token_hash"]
        for r in sql(flow.databases.identity, "SELECT token_hash FROM refresh_tokens")
    }
    assert hashlib.sha256(flow.refresh_tokens[0].encode()).hexdigest() in stored
    document = sql(
        flow.databases.contracts,
        "SELECT raw_document FROM contract_version WHERE contract_id = %s",
        flow.contract_id,
    )
    assert flow.contract_secret.encode() in bytes(document[0]["raw_document"])
    rows = outbox_rows(flow)
    assert len(rows["identity"]) >= 6  # bootstrap, rotation, logout, three folders (+ rehash)
    assert len(rows["contracts"]) == 1
    assert captured_logs.lines, "no structlog line was captured: the log scan would prove nothing"
    assert flow.wrong_login_status == 401
    assert flow.wrong_login_body
    assert flow.rejected_token_status == 401
    assert any("token_rejected" in line for line in captured_logs.lines)
    # The PAT steps really happened (AUTH-06): used on both services, then dead after revocation,
    # and every form of its secret is on the scan list (removing a needle must fail this test).
    assert len(flow.pat_tokens) == 1
    assert flow.pat_identity_status == 200
    assert flow.pat_contracts_status == 200
    assert flow.pat_revoke_status == 204
    assert flow.revoked_pat_status == 401
    assert flow.revoked_pat_body
    pat_needles = flow.secrets_to_hide()
    assert flow.pat_tokens[0] in pat_needles.values()
    assert flow.pat_tokens[0].split("_", 2)[2] in pat_needles.values()
    assert hashlib.sha256(flow.pat_tokens[0].split("_", 2)[2].encode()).hexdigest() in (
        pat_needles.values()
    )
    assert any("PAT" in label for label in pat_needles)
    stored_pat = sql(flow.databases.identity, "SELECT public_id, secret_hash FROM api_credentials")
    assert len(stored_pat) == 1
    assert stored_pat[0]["secret_hash"] in pat_needles.values()


async def test_no_secret_reaches_outbox_audit_logs_or_error_bodies(
    completed_flow: Any,
    captured_logs: Any,
    replay_to_audit: Callable[[Any], Any],
    sql: Any,
    outbox_rows: Callable[[Any], Any],
    dump: Callable[[Any], str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    flow = completed_flow
    stored = await replay_to_audit(flow)
    rows = outbox_rows(flow)
    assert stored == len(rows["identity"]) + len(rows["contracts"])

    haystacks = {
        "identity outbox": dump(rows["identity"]),
        "contracts outbox": dump(rows["contracts"]),
        "audit_event rows": dump(
            sql(flow.databases.audit, "SELECT * FROM audit_event ORDER BY id")
        ),
        "audit_dead_letter rows": dump(
            sql(flow.databases.audit, "SELECT * FROM audit_dead_letter")
        ),
        "captured log lines": "\n".join(captured_logs.lines),
        "stdlib log records": caplog.text,
        "wrong-password 401 body": flow.wrong_login_body,
        "invalid-token 401 body": flow.rejected_token_body,
        "revoked-PAT 401 body": flow.revoked_pat_body,
    }
    assert _hits(haystacks, _needles(flow, sql)) == []


async def test_password_rotation_and_logout_events_carry_exactly_the_allow_listed_keys(
    completed_flow: Any, replay_to_audit: Callable[[Any], Any], sql: Any
) -> None:
    flow = completed_flow
    await replay_to_audit(flow)
    events = sql(
        flow.databases.audit, "SELECT * FROM audit_event WHERE service = 'identity' ORDER BY id"
    )

    rotated = [e for e in events if e["action"] == "user.password_rotated"]
    assert len(rotated) == 1
    assert set(rotated[0]["before"]) == {"must_rotate", "password_set", "active_sessions"}
    assert set(rotated[0]["after"]) == {"must_rotate", "password_set", "active_sessions"}

    logged_out = [e for e in events if e["action"] == "session.logged_out"]
    assert len(logged_out) == 1
    assert set(logged_out[0]["before"]) == {"status", "user_id"}
    assert set(logged_out[0]["after"]) == {"status", "user_id", "reason"}
