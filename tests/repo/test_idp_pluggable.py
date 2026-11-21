"""Adding Entra is configuration only for contracts, validation and audit (AUTH-03, criterion 5).

Those three services authenticate through ``dc_auth`` and ask identity for decisions. Their
source must therefore contain no Entra, Graph or opaque-token (``dcp_``) specific code; a later
plan that leaks any of it into them fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SERVICE_SRC = (ROOT / "contracts" / "src", ROOT / "validation" / "src", ROOT / "audit" / "src")
# ``entra_group`` is the one allowed token: it is a member-type value of identity's wire contract
# (``dc_auth.authz_wire.MEMBER_TYPES``) that card members store; it carries no Entra logic.
IDP_SPECIFIC = re.compile(
    r"(?i)\bentra\b|entra_(?!group\b)|entra:|dcp_|graph\.microsoft|transitivememberof"
)


def _files(*roots: Path) -> list[Path]:
    return sorted(
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )


def _offenders(*roots: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in _files(*roots):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        match = IDP_SPECIFIC.search(text)
        if match:
            found[str(path.relative_to(ROOT))] = match.group(0)
    return found


def test_scan_sees_the_service_sources() -> None:
    """A vacuous scan (moved sources) must not pass silently."""
    assert len(_files(*SERVICE_SRC)) >= 20


def test_other_services_contain_no_entra_or_opaque_token_code() -> None:
    assert _offenders(*SERVICE_SRC) == {}


@pytest.mark.parametrize(
    "planted",
    [
        "entra",
        "ENTRA_TENANT",
        "entra:tenant",
        "dcp_abc_secret",
        "https://graph.microsoft.com/v1.0",
        "transitiveMemberOf",
    ],
)
def test_the_scanner_flags_a_planted_string(tmp_path: Path, planted: str) -> None:
    (tmp_path / "leak.py").write_text(f"X = '{planted}'\n", encoding="utf-8")
    flagged = _offenders_in(tmp_path)
    assert flagged, f"scanner missed {planted!r}"


def test_the_scanner_allows_only_the_entra_group_member_type_value(tmp_path: Path) -> None:
    (tmp_path / "member.py").write_text("T = 'entra_group'\n", encoding="utf-8")
    assert _offenders_in(tmp_path) == []
    (tmp_path / "leak.py").write_text("T = 'entra_groups_tenant'\n", encoding="utf-8")
    assert _offenders_in(tmp_path) == ["leak.py"]


def test_the_scanner_does_not_flag_a_clean_file(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text("X = 'contracts and folders'\n", encoding="utf-8")
    assert _offenders_in(tmp_path) == []


def _offenders_in(directory: Path) -> list[str]:
    hits = []
    for path in _files(directory):
        if IDP_SPECIFIC.search(path.read_text(encoding="utf-8")):
            hits.append(path.name)
    return hits
