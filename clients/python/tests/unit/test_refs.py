"""The reference grammar and its resolution against a fake server."""

from __future__ import annotations

import warnings

import pytest
from hypothesis import given
from hypothesis import strategies as st
from support.fake_server import FakeContractum, FakeVersion

from contractum.client import Client
from contractum.errors import (
    AmbiguousReferenceError,
    NotFoundError,
    ServerError,
    UsageError,
)
from contractum.refs import (
    ContractRef,
    is_folder_path,
    is_version,
    parse_ref,
    parse_version,
    resolve_ref,
    resolve_version,
)

BASE = "https://contractum.example"
UUID_TEXT = "3f2c1b9e-6a4d-4c58-9d0e-2b7a1c5e8f10"
DOC = b"apiVersion: v3.0.2\nkind: DataContract\nid: payments.orders\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("payments.orders", ContractRef(None, "payments.orders", None)),
        ("payments.orders@1.4.0", ContractRef(None, "payments.orders", "1.4.0")),
        ("payments.orders@2.0.0-rc.1", ContractRef(None, "payments.orders", "2.0.0-rc.1")),
        ("payments.orders@1.0.0+build.5", ContractRef(None, "payments.orders", "1.0.0+build.5")),
        ("payments.orders@preview", ContractRef(None, "payments.orders", "preview")),
        ("payments.orders@LATEST", ContractRef(None, "payments.orders", "latest")),
        (
            "acme/payments/ledger:payments.orders@latest",
            ContractRef("acme/payments/ledger", "payments.orders", "latest"),
        ),
        (f"{UUID_TEXT}@preview", ContractRef(None, UUID_TEXT, "preview")),
        (f"  {UUID_TEXT.upper()}  ", ContractRef(None, UUID_TEXT, None)),
        ("acme:orders", ContractRef("acme", "orders", None)),
    ],
)
def test_parse_accepts_the_documented_forms(text: str, expected: ContractRef) -> None:
    assert parse_ref(text) == expected


@pytest.mark.parametrize(
    ("text", "ident"),
    [
        ("orders@team", "orders@team"),
        ("user@example.com", "user@example.com"),
        ("URN:orders", "URN:orders"),
        ("orders@v", "orders@v"),
    ],
)
def test_an_at_suffix_or_prefix_that_is_not_a_version_or_folder_stays_in_the_identifier(
    text: str, ident: str
) -> None:
    parsed = parse_ref(text)
    assert parsed.ident == ident
    assert parsed.version is None
    assert parsed.folder_path is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "orders@",
        "orders@1.4",
        "orders@v1.0.0",
        "orders@1.0.0-",
        "Acme/x:orders",
        ":orders",
        "acme:",
        "a//b:orders",
        "acme/Payments:orders",
        "orders with space",
        "orders\x00",
        f"acme:{UUID_TEXT}",
        "x" * 600,
        "i" * 300,
        "@1.0.0",
        f"{'a' * 64}/b:orders",
    ],
)
def test_malformed_references_are_usage_errors(text: str) -> None:
    with pytest.raises(UsageError):
        parse_ref(text)


@given(st.text(max_size=80))
def test_any_text_parses_or_raises_usage_error(text: str) -> None:
    try:
        parsed = parse_ref(text)
    except UsageError:
        return
    assert parsed.ident
    assert str(parsed)


@given(st.from_regex(r"[a-z0-9@:/.+\- ]{0,60}", fullmatch=True))
def test_reference_shaped_text_never_raises_anything_else(text: str) -> None:
    try:
        parse_ref(text)
    except UsageError:
        pass


def test_helpers_classify_versions_and_folder_paths() -> None:
    assert is_version("1.0.0")
    assert is_version("preview")
    assert not is_version("1.0")
    assert is_folder_path("acme/payments")
    assert not is_folder_path("acme/")
    assert parse_version(" LATEST ") == "latest"
    assert parse_version("2.0.0") == "2.0.0"
    with pytest.raises(UsageError):
        parse_version("v2")
    with pytest.raises(UsageError):
        parse_version("1" * 600)


def test_str_round_trips_the_parts() -> None:
    assert str(ContractRef("a/b", "x", "1.0.0")) == "a/b:x@1.0.0"
    assert str(ContractRef(None, "x", None)) == "x"


# --- resolution -----------------------------------------------------------------------------


def _client(canary_token: str) -> Client:
    return Client(BASE, token=canary_token, sleep=lambda seconds: None)


def test_a_uuid_resolves_through_get_contract(canary_token: str) -> None:
    server = FakeContractum()
    contract = server.add_contract("payments.orders", [FakeVersion("1.0.0", DOC, status="latest")])
    with server, _client(canary_token) as client:
        resolved = resolve_ref(client, parse_ref(contract.contract_id))
        targets = server.targets()
    assert str(resolved.contract_id) == contract.contract_id
    assert resolved.odcs_id == "payments.orders"
    assert resolved.name == "Payments.Orders"
    assert targets == [f"GET /api/v1/contracts/contracts/{contract.contract_id}"]


def test_a_folder_path_resolves_through_the_folder_then_the_listing(canary_token: str) -> None:
    server = FakeContractum()
    folder = server.add_folder("acme/payments/ledger")
    other = server.add_folder("acme/sales")
    mine = server.add_contract(
        "payments.orders", [FakeVersion("1.0.0", DOC, status="latest")], folder_id=folder
    )
    server.add_contract(
        "payments.orders", [FakeVersion("1.0.0", DOC, status="latest")], folder_id=other
    )
    with server, _client(canary_token) as client:
        resolved = resolve_ref(client, parse_ref("acme/payments/ledger:payments.orders"))
        targets = server.targets()
    assert str(resolved.contract_id) == mine.contract_id
    assert targets[0].startswith("GET /api/v1/identity/folders?")
    assert "path=acme%2Fpayments%2Fledger" in targets[0]
    assert f"folder_id={folder}" in targets[1]
    assert "include_subtree=false" in targets[1].lower()


def test_an_unknown_folder_is_not_found(canary_token: str) -> None:
    with FakeContractum() as server, _client(canary_token) as client:
        with pytest.raises(NotFoundError, match="acme/missing"):
            resolve_ref(client, parse_ref("acme/missing:orders"))
        assert len(server.requests) == 1


def test_zero_hits_name_the_reference(canary_token: str) -> None:
    with FakeContractum(), _client(canary_token) as client:
        with pytest.raises(NotFoundError, match="payments.orders@1.0.0"):
            resolve_ref(client, parse_ref("payments.orders@1.0.0"))


def test_a_listing_hit_for_another_odcs_id_is_not_taken(canary_token: str) -> None:
    server = FakeContractum()
    server.add_contract("payments.orders-extra", [FakeVersion("1.0.0", DOC, status="latest")])
    server.loose_odcs_filter = True
    with server, _client(canary_token) as client:
        with pytest.raises(NotFoundError):
            resolve_ref(client, parse_ref("payments.orders"))


def test_two_hits_are_ambiguous_and_name_every_candidate_and_the_hint(canary_token: str) -> None:
    server = FakeContractum()
    first = server.add_contract(
        "payments.orders",
        [FakeVersion("1.0.0", DOC, status="latest")],
        name="Orders A",
        folder_id="aaaaaaaa-0000-4000-8000-000000000001",
    )
    second = server.add_contract(
        "payments.orders",
        [FakeVersion("1.0.0", DOC, status="latest")],
        name="Orders B",
        folder_id="bbbbbbbb-0000-4000-8000-000000000002",
    )
    with server, _client(canary_token) as client, pytest.raises(AmbiguousReferenceError) as raised:
        client.get("payments.orders")
    error = raised.value
    assert first.contract_id in str(error)
    assert second.contract_id in str(error)
    assert "Orders A" in str(error)
    assert "bbbbbbbb-0000-4000-8000-000000000002" in str(error)
    assert len(error.candidates) == 2
    assert error.hint is not None
    assert "<folder-path>:payments.orders" in error.hint
    assert "UUID" in error.hint
    assert error.exit_code == 2


def test_a_folder_path_makes_a_shared_odcs_id_unambiguous(canary_token: str) -> None:
    server = FakeContractum()
    folder_a = server.add_folder("acme/a")
    folder_b = server.add_folder("acme/b")
    server.add_contract(
        "payments.orders", [FakeVersion("1.0.0", DOC, status="latest")], folder_id=folder_a
    )
    wanted = server.add_contract(
        "payments.orders",
        [FakeVersion("2.0.0", DOC + b"# b\n", status="latest")],
        folder_id=folder_b,
    )
    with server, _client(canary_token) as client:
        version = client.get("acme/b:payments.orders")
    assert version.contract_id == wanted.contract_id
    assert version.semver == "2.0.0"


def test_resolve_version_returns_the_exact_semver(canary_token: str) -> None:
    server = FakeContractum()
    contract = server.add_contract(
        "payments.orders",
        [FakeVersion("1.2.0", DOC, status="latest"), FakeVersion("1.1.0", DOC + b"#\n")],
    )
    from uuid import UUID

    cid = UUID(contract.contract_id)
    with server, _client(canary_token) as client:
        assert resolve_version(client, cid, None) == "1.2.0"
        assert resolve_version(client, cid, "latest", label="x") == "1.2.0"
        assert resolve_version(client, cid, "1.1.0") == "1.1.0"


def test_a_server_that_answers_another_version_is_refused(canary_token: str) -> None:
    server = FakeContractum()
    contract = server.add_contract("payments.orders", [FakeVersion("1.2.0", DOC, status="latest")])
    from uuid import UUID

    with server, _client(canary_token) as client:
        server.contracts[0].versions[0].semver = "9.9.9"
        server._pick = lambda c, ref: c.versions[0]  # type: ignore[method-assign]
        with pytest.raises(ServerError, match="9.9.9"):
            resolve_version(client, UUID(contract.contract_id), "1.2.0")


def test_preview_picks_the_highest_precedence_preview_and_fetches_it(canary_token: str) -> None:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [
            FakeVersion("1.2.0", DOC, status="latest"),
            FakeVersion("1.3.0", DOC + b"# 130\n", status="preview"),
            FakeVersion("1.1.0", DOC + b"# 110\n"),
        ],
    )
    with server, _client(canary_token) as client:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            version = client.get("payments.orders@preview")
    assert version.semver == "1.3.0"
    assert version.status == "preview"
    assert not any("/versions/latest" in path for path in server.paths())


def test_several_previews_warn_naming_both_and_the_chosen_one(canary_token: str) -> None:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [
            FakeVersion("1.2.0", DOC, status="latest"),
            FakeVersion("1.3.0", DOC + b"# 130\n", status="preview"),
            FakeVersion("2.0.0", DOC + b"# 200\n", status="preview"),
        ],
    )
    with server, _client(canary_token) as client:
        with pytest.warns(UserWarning, match="2.0.0") as caught:
            version = client.get("payments.orders@preview")
    assert version.semver == "2.0.0"
    text = str(caught[0].message)
    assert "1.3.0" in text
    assert "using 2.0.0" in text


def test_no_preview_is_not_found_and_latest_is_never_fetched(canary_token: str) -> None:
    server = FakeContractum()
    server.add_contract("payments.orders", [FakeVersion("1.2.0", DOC, status="latest")])
    with server, _client(canary_token) as client, pytest.raises(NotFoundError, match="no Preview"):
        client.get("payments.orders@preview")
    joined = " ".join(server.targets())
    assert "/versions/latest" not in joined
    assert "/versions/1.2.0" not in joined
    assert joined.count("/versions") == 1


def test_an_exact_semver_fetches_that_version_and_never_falls_back(canary_token: str) -> None:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [FakeVersion("1.2.0", DOC, status="latest"), FakeVersion("1.1.0", DOC + b"# 110\n")],
    )
    with server, _client(canary_token) as client:
        version = client.get("payments.orders@1.1.0")
        assert version.semver == "1.1.0"
        assert version.status == "older"
        with pytest.raises(NotFoundError):
            client.get("payments.orders@1.0.0")
    assert not any(path.endswith("/versions/latest/metadata") for path in server.paths())


def test_the_version_argument_overrides_the_reference_version(canary_token: str) -> None:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [FakeVersion("1.2.0", DOC, status="latest"), FakeVersion("1.1.0", DOC + b"# 110\n")],
    )
    with server, _client(canary_token) as client:
        assert client.get("payments.orders@1.2.0", version="1.1.0").semver == "1.1.0"
        assert client.get("payments.orders@1.1.0", version="latest").semver == "1.2.0"
        with pytest.raises(UsageError):
            client.get("payments.orders", version="v1")


def test_a_contract_without_a_latest_is_not_found(canary_token: str) -> None:
    server = FakeContractum()
    server.add_contract("payments.orders", [FakeVersion("2.0.0", DOC, status="preview")])
    with server, _client(canary_token) as client, pytest.raises(NotFoundError):
        client.get("payments.orders")
    assert not any(path.endswith("/versions/2.0.0") for path in server.paths())
