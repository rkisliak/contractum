"""Load the JSON response bodies in tests/fixtures/payloads by name."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DIRECTORY = Path(__file__).resolve().parents[1] / "fixtures" / "payloads"


def load_payload(name: str) -> Any:
    """Return the parsed body of ``tests/fixtures/payloads/<name>.json``."""
    return json.loads((_DIRECTORY / f"{name}.json").read_text(encoding="utf-8"))
