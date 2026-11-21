"""SUB-07 / D-98 / T-04.1-30: the webhook secret is visible exactly once.

A manager enables a webhook (secret S1, returned once), a notice is delivered, the secret is
rotated (S2, returned once) and another notice is delivered; two refused calls on another card
(422 invalid-webhook-url, 409 webhook-not-configured) and the ordinary reads and edits follow. The
needles are S1, S2, their base64 key parts, the hex of their key bytes and both Fernet tokens read
back from ``card_webhook_secret``. None may appear in a contracts outbox row, a notice or delivery
row, captured log output, a problem body or any response other than the single creating response
and the single rotating response (which are the only places the secret is meant to be).

Modelled on ``tests/integration/test_no_secrets_in_audit.py``; failures name the store and the
needle's label only, never the needle. The scan list is never to be weakened: a hit is fixed at its
source.
"""

from __future__ import annotations

import base64
import io
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from cryptography.fernet import Fernet
from dc_contracts import webhooks
from dc_core.logging import REDACTED, configure_logging
from pydantic import SecretStr

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"
KEY = Fernet.generate_key().decode()

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

SETTINGS: dict[str, Any] = {
    "webhook_secret_key": KEY,
    "webhook_require_https": False,
    "webhook_allowed_private_hosts": ["localhost"],
    # The worker reads the mandatory allow-list from identity with the notifier key.
    "with_notifier": True,
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one resolver seam: the sink is ``localhost``, one name is a metadata address."""

    async def fake(host: str, port: int) -> list[str]:
        if host == "localhost":
            return ["127.0.0.1"]
        if host == "metadata.example.test":
            return ["169.254.169.254"]
        raise OSError("no such host")

    monkeypatch.setattr(webhooks, "_resolve", fake)


class LogCapture(io.StringIO):
    """dc_core's structlog JSON output, line by line, at DEBUG so nothing is filtered out."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def write(self, s: str) -> int:
        self.lines.extend(line for line in s.splitlines() if line)
        return super().write(s)

    def install(self) -> None:
        """The apps configure logging in their lifespans: install after the stack started."""
        configure_logging("contracts-scan", "DEBUG", stream=self)


@pytest.fixture
def captured_logs() -> Iterator[LogCapture]:
    capture = LogCapture()
    try:
        yield capture
    finally:
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()


def _hits(haystacks: dict[str, str], needles: dict[str, str]) -> list[str]:
    return [
        f"{store} contains {label}"
        for store, text in haystacks.items()
        for label, needle in needles.items()
        if needle in text
    ]


def dump(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def test_the_scanner_reports_a_planted_needle() -> None:
    hits = _hits({"store": "x whsec_PLANTED y", "clean": "nothing"}, {"S1": "whsec_PLANTED"})
    assert hits == ["store contains S1"]


@dataclass
class Flow:
    s1: str = ""
    s2: str = ""
    creating_response: str = ""
    rotating_response: str = ""
    bodies: dict[str, str] = field(default_factory=dict)
    card_id: str = ""
    other_card_id: str = ""
    contract: str = ""


async def run_flow(stack: Any, sql: Sql, sink: Any, logs: LogCapture) -> Flow:
    flow = Flow()
    chain = await stack.chain()
    contract, _ = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
    flow.contract = contract
    token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "consumer", chain.team)
    card = await stack.create_card(chain.team, token=token, name="orders consumers")
    flow.card_id = card["id"]
    logs.install()

    enabled = await stack.client.patch(
        f"{CARDS}/{card['id']}",
        json={"webhook_url": sink.url("/hooks/token-abc"), "webhook_enabled": True},
        headers=bearer(token),
    )
    assert enabled.status_code == 200, enabled.text
    flow.s1 = enabled.json()["webhook_secret"]
    flow.creating_response = enabled.text
    pinned = await stack.subscribe(card["id"], contract, "2.3.0")
    assert pinned.status_code == 201, pinned.text

    # First notice, delivered with S1.
    deprecated = await stack.client.post(
        f"/api/v1/contracts/contracts/{contract}/versions/2.3.0/deprecate",
        json={"sunset_date": (datetime.now(UTC) + timedelta(days=60)).date().isoformat()},
        headers=bearer(stack.admin_token),
    )
    assert deprecated.status_code == 200, deprecated.text
    assert await stack.pump_notices() == 1
    assert await stack.contracts_app.state.card_notifier.run_once() == 1

    for label, path in (("card read", ""), ("notices read", "/notices")):
        read = await stack.client.get(f"{CARDS}/{card['id']}{path}", headers=bearer(token))
        assert read.status_code == 200, read.text
        flow.bodies[f"GET {label} before rotation"] = read.text

    # Rotation: S2 is returned once, S1 stays as the overlap secret.
    rotated = await stack.client.post(f"{CARDS}/{card['id']}/webhook-secret", headers=bearer(token))
    assert rotated.status_code == 200, rotated.text
    flow.s2 = rotated.json()["webhook_secret"]
    flow.rotating_response = rotated.text
    assert flow.s2 != flow.s1

    # Second notice during the overlap (both secrets sign), seeded straight into the tables.
    notice = sql(
        stack.contracts_db_url,
        "INSERT INTO card_notice (card_id, event_type, contract_id, payload, dedupe_key, "
        "occurred_at) VALUES (%s, 'contract.version.deprecated', %s, %s::jsonb, %s, now()) "
        "RETURNING id",
        card["id"],
        contract,
        json.dumps({"contract": {"id": contract, "odcs_id": "urn:x"}}),
        f"seed:{uuid.uuid4()}",
    )[0]["id"]
    sql(
        stack.contracts_db_url,
        "INSERT INTO card_notice_delivery (notice_id, channel) VALUES (%s, 'webhook')",
        notice,
    )
    assert await stack.contracts_app.state.card_notifier.run_once() == 1

    # Ordinary reads and an edit without a new secret.
    for label, path in (("card read", ""), ("notices read", "/notices")):
        read = await stack.client.get(f"{CARDS}/{card['id']}{path}", headers=bearer(token))
        assert read.status_code == 200, read.text
        flow.bodies[f"GET {label} after rotation"] = read.text
    edited = await stack.client.patch(
        f"{CARDS}/{card['id']}", json={"description": "edited"}, headers=bearer(token)
    )
    assert edited.status_code == 200, edited.text
    assert edited.json().get("webhook_secret") is None
    flow.bodies["PATCH without a new secret"] = edited.text

    # Refusals on another card: 422 for a metadata address, 409 for a rotation without a URL.
    other = await stack.create_card(chain.team, token=token, name="other consumers")
    flow.other_card_id = other["id"]
    refused = await stack.client.patch(
        f"{CARDS}/{other['id']}",
        json={"webhook_url": "http://metadata.example.test/latest", "webhook_enabled": True},
        headers=bearer(token),
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"] == "urn:dc:problem:invalid-webhook-url"
    flow.bodies["422 invalid-webhook-url"] = refused.text
    unconfigured = await stack.client.post(
        f"{CARDS}/{other['id']}/webhook-secret", headers=bearer(token)
    )
    assert unconfigured.status_code == 409, unconfigured.text
    assert unconfigured.json()["type"] == "urn:dc:problem:webhook-not-configured"
    flow.bodies["409 webhook-not-configured"] = unconfigured.text

    # The log pipeline in the running process redacts a secret by value under a harmless key.
    structlog.get_logger("scan").info("probe", note=f"the secret is {flow.s1}")
    return flow


def _needles(flow: Flow, sql: Sql, stack: Any) -> dict[str, str]:
    needles: dict[str, str] = {}
    for label, secret in (("S1", flow.s1), ("S2", flow.s2)):
        key_part = secret.removeprefix("whsec_")
        needles[f"{label} secret"] = secret
        needles[f"{label} base64 key part"] = key_part
        needles[f"{label} key hex"] = base64.b64decode(key_part, validate=True).hex()
    rows = sql(stack.contracts_db_url, "SELECT secret_enc, previous_enc FROM card_webhook_secret")
    for index, row in enumerate(rows):
        needles[f"current Fernet token #{index}"] = row["secret_enc"]
        if row["previous_enc"]:
            needles[f"previous Fernet token #{index}"] = row["previous_enc"]
    return needles


async def test_the_webhook_secret_never_leaks_after_it_was_shown_once(
    make_stack: MakeStack,
    sql: Sql,
    webhook_sink: Any,
    captured_logs: LogCapture,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with make_stack(**SETTINGS) as stack:
        flow = await run_flow(stack, sql, webhook_sink, captured_logs)
        needles = _needles(flow, sql, stack)
        db = stack.contracts_db_url
        outbox = sql(db, "SELECT * FROM outbox ORDER BY id")
        notices = sql(db, "SELECT * FROM card_notice ORDER BY id")
        delivery_rows = sql(db, "SELECT * FROM card_notice_delivery ORDER BY id")
        cards = sql(db, "SELECT * FROM subscriber_card ORDER BY id")
        secret_rows = sql(db, "SELECT * FROM card_webhook_secret")

        # The scan is not vacuous: the stores are populated and the needles are the real secrets.
        assert any(row["action"] == "card.webhook_secret_rotated" for row in outbox)
        assert len(notices) >= 2
        assert [row["status"] for row in delivery_rows] == ["delivered", "delivered"]
        assert len(webhook_sink.requests) == 2
        assert any("card_notice_delivery" in line for line in captured_logs.lines), (
            "no delivery log line was captured: the log scan would prove nothing"
        )
        assert len(secret_rows) == 1 and secret_rows[0]["previous_enc"]
        box = webhooks.SecretBox(SecretStr(KEY))
        assert box.open(secret_rows[0]["secret_enc"]) == flow.s2
        assert box.open(secret_rows[0]["previous_enc"]) == flow.s1
        assert flow.s1 in flow.creating_response  # the one place S1 is meant to appear
        assert flow.s2 in flow.rotating_response  # the one place S2 is meant to appear
        assert {"422 invalid-webhook-url", "409 webhook-not-configured"} <= set(flow.bodies)
        assert len(needles) >= 8
        assert not any(needle in dump(row) for row in cards for needle in needles.values())

        sink_traffic = "\n".join(
            dump({"headers": r.headers, "body": r.body.decode()}) for r in webhook_sink.requests
        )
        haystacks = {
            "contracts outbox rows": dump(outbox),
            "card_notice rows": dump(notices),
            "card_notice_delivery rows": dump(delivery_rows),
            "subscriber_card rows": dump(cards),
            "captured log lines": "\n".join(captured_logs.lines),
            "stdlib log records": caplog.text,
            "process stdout and stderr": "".join(capsys.readouterr()),
            "requests the receiver got": sink_traffic,
            **{f"response: {label}": body for label, body in flow.bodies.items()},
        }
        assert _hits(haystacks, needles) == []
        # The self-check on the live data: a needle planted in one store is reported there.
        planted = _hits({**haystacks, "planted": f"noise {flow.s1} noise"}, needles)
        assert "planted contains S1 secret" in planted
        assert all(hit.startswith("planted ") for hit in planted)


async def test_a_secret_logged_by_value_is_redacted_in_the_running_pipeline(
    make_stack: MakeStack,
    sql: Sql,
    webhook_sink: Any,
    captured_logs: LogCapture,
) -> None:
    async with make_stack(**SETTINGS) as stack:
        flow = await run_flow(stack, sql, webhook_sink, captured_logs)
        probe = [line for line in captured_logs.lines if '"probe"' in line]
        assert len(probe) == 1
        assert flow.s1.removeprefix("whsec_") not in probe[0]
        assert json.loads(probe[0])["note"] == f"the secret is whsec_{REDACTED}"


def test_the_secret_response_models_are_the_only_ones_with_a_secret_field() -> None:
    """Static guard: no other response model of the cards API can carry ``webhook_secret``."""
    from dc_contracts.api import cards

    holders = sorted(
        name
        for name, obj in vars(cards).items()
        if isinstance(obj, type)
        and hasattr(obj, "model_fields")
        and "webhook_secret" in obj.model_fields
    )
    assert holders, "the creating and rotating responses must exist"
    assert len(holders) <= 2, holders
