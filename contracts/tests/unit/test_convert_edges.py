"""Edges of representation negotiation, conversion, the search cursor and stored SemVer parts."""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import pytest
from dc_contracts.convert import negotiate, render
from dc_contracts.lifecycle import parts_of
from dc_contracts.search import decode_cursor
from dc_core.problems import ProblemError

pytestmark = pytest.mark.unit

YAML_DOC = b"apiVersion: v3.2.0\nkind: DataContract\nid: x\n"


def test_media_range_parameters_other_than_q_are_ignored() -> None:
    accept = "application/json; charset=utf-8; q=0.9, application/yaml; q=0.5"
    assert negotiate(accept, "application/yaml") == "json"


def test_render_refuses_an_unknown_target() -> None:
    with pytest.raises(ValueError, match="unknown conversion target 'xml'"):
        render(YAML_DOC, "application/yaml", "xml")


def test_a_cursor_for_the_uppercase_spelling_of_an_id_is_not_canonical() -> None:
    contract_id = uuid.uuid4()
    upper = base64.urlsafe_b64encode(str(contract_id).upper().encode()).decode().rstrip("=")

    with pytest.raises(ProblemError) as exc:
        decode_cursor(upper)

    assert (exc.value.status, exc.value.slug) == (422, "validation-error")


@pytest.mark.parametrize("missing", ["semver", "semver_major", "semver_minor", "semver_patch"])
def test_a_non_draft_row_without_its_semver_columns_is_a_programming_error(missing: str) -> None:
    columns = {
        "semver": "1.2.3",
        "semver_major": 1,
        "semver_minor": 2,
        "semver_patch": 3,
        "semver_prerelease": None,
    }
    row = SimpleNamespace(**{**columns, missing: None})

    with pytest.raises(ValueError, match="must carry its semver columns"):
        parts_of(row)  # type: ignore[arg-type]
