"""The command sets the whole-surface proofs (read-only fence, secret hygiene) both drive."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from support.odcs import contract_doc
from support.runs import BASE
from support.session import Session

from contractum import Client


@dataclass(frozen=True)
class Credentials:
    """The secret one run signs in with."""

    pat: str


def sign_in_flow(session: Session, creds: Credentials) -> None:
    """Token sign-in with whoami and logout, plus the refused password options."""
    session.run("login", "--token", "-", stdin=creds.pat)
    session.run("whoami")
    session.run("whoami", "--json", label="whoami --json")
    session.run("logout", label="logout of a token", backed=False)
    session.run(
        "login", "--username", "alice", expect=2, backed=False, label="login --username refused"
    )


def read_flow(
    session: Session, tmp_path: Path, data: Path, creds: Credentials, *, latest_exit: int = 0
) -> None:
    """Every read command against ``payments.orders``; ``latest_exit`` is the code for ``data``."""
    contracts, lock = str(tmp_path / "pulled"), str(tmp_path / "contractum.lock")
    session.run("login", "--token", "-", stdin=creds.pat, label="login again")
    session.run("list")
    session.run("list", "--all", "--json", label="list --all --json")
    session.run("versions", "payments.orders")
    session.run("show", "payments.orders")
    session.run("show", "payments.orders", "--metadata", label="show --metadata")
    session.run("show", "payments.orders", "--report", label="show --report")
    pull = ["--dir", contracts, "--lock", lock]
    session.run("pull", "payments.orders", *pull)
    session.run("pull", "--frozen", *pull, label="pull --frozen")
    target = [str(data), "--contract", "payments.orders"]
    session.run("test", *target, expect=latest_exit)
    session.run("test", *target, "--version", "preview", expect=1, label="test preview")
    pinned = [*target, "--frozen", "--lock", lock]
    session.run("test", *pinned, expect=latest_exit, label="test --frozen")
    session.run(
        "test", *pinned, "--offline", expect=latest_exit, backed=False, label="test --offline"
    )
    document = tmp_path / "orders.yaml"
    document.write_text(yaml.safe_dump(contract_doc()), encoding="utf-8")
    session.run(
        "test",
        str(data),
        "--contract-file",
        str(document),
        expect=latest_exit,
        backed=False,
        label="test --contract-file",
    )
    session.run("compare", *target, expect=1)


def config_flow(session: Session) -> None:
    """The profile commands; none of them talks to the server."""
    session.run("config", "set", "url", BASE, backed=False, label="config set")
    session.run("config", "get", "url", backed=False, label="config get")
    session.run("config", "list", backed=False, label="config list")
    session.run("--profile", "work", "config", "set", "url", BASE, backed=False, label="set work")
    session.run("config", "use", "work", backed=False, label="config use")


def client_methods(client: Client) -> None:
    """Call every public read method of the Client once."""
    client.whoami()
    client.auth_policy()
    assert client.find_folder("acme/payments") is not None
    client.search(folder="acme/payments")
    contract_id = client.search("orders")[0].id
    client.list_contracts()
    list(client.iter_contracts())
    client.get_contract(contract_id)
    client.list_versions(contract_id)
    client.version_metadata(contract_id, "1.1.0")
    client.fetch_document(contract_id, "1.1.0")
    client.validation_report(contract_id, "latest")
    client.diff(contract_id, "1.1.0", "2.0.0")
    client.locate("payments.orders")
    client.get("payments.orders")
    client.versions("payments.orders")
