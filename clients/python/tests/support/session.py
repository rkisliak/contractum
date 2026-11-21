"""One recording fake server and a command session shared by the whole-surface proofs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from support.auth import Outcome, run_main
from support.payloads import load_payload
from support.runs import BASE, orders_server

ME = "/api/v1/identity/me"
POLICY = "/api/v1/identity/auth/policy"
REPORT: dict[str, Any] = {
    "status": "completed",
    "structural": {"valid": True, "error_count": 0, "errors": []},
    "findings": [],
    "summary": {"publishable": True, "blocking_count": 0},
}


def recording_server() -> Any:
    """``payments.orders`` in three versions plus the identity endpoints a sign-in touches."""
    fake = orders_server(report=REPORT)
    fake.add_folder("acme/payments")
    me = {**load_payload("me"), "via": "pat", "read_only": True}
    fake.router.get(ME).respond(200, json=me)
    fake.router.get(POLICY).respond(200, json=load_payload("auth_policy"))
    return fake


@dataclass
class Session:
    """Runs commands against the server and records how many requests each one caused."""

    server: Any
    capsys: pytest.CaptureFixture[str]
    monkeypatch: pytest.MonkeyPatch
    reached: dict[str, int] = field(default_factory=dict)
    local: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    after: Callable[[], None] | None = None
    """Called after every command, for checks that must hold at every step."""

    def run(
        self, *argv: str, stdin: str = "", expect: int = 0, backed: bool = True, label: str = ""
    ) -> Outcome:
        name = label or " ".join(argv)
        before = len(self.server.requests)
        outcome = run_main(
            ["--url", BASE, *argv], capsys=self.capsys, monkeypatch=self.monkeypatch, stdin=stdin
        )
        assert outcome.code == expect, f"{name}: {outcome.output}"
        self.outputs.extend([outcome.stdout, outcome.stderr])
        if self.after is not None:
            self.after()
        if backed:
            self.reached[name] = len(self.server.requests) - before
        else:
            self.local.append(name)
        return outcome
