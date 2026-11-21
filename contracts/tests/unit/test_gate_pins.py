"""The pin projection stored with a published version, and its paused marker (quick 261118-vpa)."""

from __future__ import annotations

from typing import Any

import pytest
from dc_contracts.validation_client import PIN_KEYS, gate_pin

pytestmark = pytest.mark.unit

WIRE: dict[str, Any] = {
    "rule_set_id": "05d2384d-a57d-4922-a576-921b2f7113bd",
    "name": "odcs-structural",
    "folder_id": None,
    "scope": "platform",
    "version": 1,
    "source": "mandatory",
    "mode": "enforce",
    "dsl_version": "cel-1",
    "deprecated_version": False,
}


def test_an_unpaused_or_unmarked_pin_is_exactly_the_stored_keys() -> None:
    expected = {key: WIRE[key] for key in PIN_KEYS}

    assert gate_pin(WIRE) == expected
    assert gate_pin({**WIRE, "paused": False}) == expected
    assert "deprecated_version" not in gate_pin(WIRE)


def test_a_paused_pin_adds_the_paused_marker_and_keeps_the_warn_mode() -> None:
    projected = gate_pin({**WIRE, "mode": "warn", "paused": True})

    assert projected == {**{key: WIRE[key] for key in PIN_KEYS}, "mode": "warn", "paused": True}
