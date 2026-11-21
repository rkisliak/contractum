"""Every CLI command and every public Client method is read-only towards the Contractum API.

One recording fake server serves the whole session. The proof has two halves: each server-backed
command leaves at least one recorded request (a command that never reached the server proves
nothing), and every recorded request is a GET or a HEAD. Not a single POST is sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
import pytest
from support.datasets import arrow_table, orders_rows
from support.flows import Credentials, client_methods, config_flow, read_flow, sign_in_flow
from support.runs import BASE
from support.session import ME, Session, recording_server

from contractum import Client

_READS = frozenset({"GET", "HEAD"})


def violations(requests: list[httpx.Request]) -> list[str]:
    """The recorded requests the read-only fence forbids: everything but GET and HEAD."""
    return [
        f"{request.method} {request.url.path}"
        for request in requests
        if request.method not in _READS
    ]


@pytest.fixture
def server() -> Any:
    return recording_server()


@pytest.fixture
def session(
    server: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Session:
    return Session(server, capsys, monkeypatch)


def _parquet(tmp_path: Path) -> Path:
    path = tmp_path / "orders.parquet"
    pq.write_table(arrow_table(orders_rows(good=True)), path)
    return path


def test_every_command_and_client_method_stays_read_only(
    session: Session,
    server: Any,
    tmp_path: Path,
    canary_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = Credentials(canary_token)
    monkeypatch.chdir(tmp_path)
    with server:
        sign_in_flow(session, creds)
        read_flow(session, tmp_path, _parquet(tmp_path), creds)
        config_flow(session)
        before = len(server.requests)
        with Client(BASE, token=creds.pat, cache=False) as client:
            client_methods(client)
        client_requests = len(server.requests) - before

    silent = sorted(name for name, count in session.reached.items() if count < 1)
    assert silent == [], f"commands that never reached the server: {silent}"
    assert len(session.reached) >= 16
    assert len(session.local) >= 8
    assert client_requests >= 15
    assert violations(server.requests) == []
    assert [r.url.path for r in server.requests if r.method == "POST"] == []


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "POST"])
def test_the_checker_flags_every_write_it_is_given(method: str) -> None:
    planted = [httpx.Request(method, f"{BASE}/api/v1/contracts/contracts")]
    assert violations(planted) == [f"{method} /api/v1/contracts/contracts"]


def test_the_checker_accepts_reads_only() -> None:
    allowed = [httpx.Request("GET", f"{BASE}{ME}"), httpx.Request("HEAD", f"{BASE}{ME}")]
    assert violations(allowed) == []


@pytest.mark.parametrize("path", ["login", "refresh", "logout"])
def test_the_checker_flags_a_post_to_the_former_auth_endpoints(path: str) -> None:
    planted = [httpx.Request("POST", f"{BASE}/api/v1/identity/auth/{path}")]
    assert violations(planted) == [f"POST /api/v1/identity/auth/{path}"]
