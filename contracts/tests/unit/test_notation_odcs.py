"""OdcsAdapter: declared_version and the fidelity-preserving in-place copy (CONT-06, T-03-15)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from dc_contracts.notation import OdcsAdapter, get_adapter
from dc_core.problems import ProblemError
from dc_odcs import load_json, load_yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.unit

ADAPTER = OdcsAdapter()
YAML = "application/yaml"
JSON = "application/json"
BOM = "\ufeff".encode()


def copy(
    raw: bytes, *, version: str | None = None, odcs_id: str | None = None, mt: str = YAML
) -> bytes:
    return ADAPTER.copy_with(raw, mt, odcs_id=odcs_id, version=version)


def problem(raw: bytes, **kwargs: Any) -> ProblemError:
    with pytest.raises(ProblemError) as caught:
        copy(raw, **kwargs)
    return caught.value


def test_get_adapter_returns_the_odcs_adapter_and_refuses_unknown_notations() -> None:
    assert get_adapter("odcs").notation == "odcs"
    with pytest.raises(RuntimeError):
        get_adapter("avro")


def test_declared_version_is_the_string_only() -> None:
    assert ADAPTER.declared_version({"version": "1.0.0"}) == "1.0.0"
    assert ADAPTER.declared_version({"version": 1.1}) is None
    assert ADAPTER.declared_version({"version": 1}) is None
    assert ADAPTER.declared_version({}) is None


def test_block_yaml_plain_version_changes_only_the_token_and_keeps_the_comment() -> None:
    source = (
        b"# header comment\napiVersion: v3.2.0\nkind: DataContract\nid: urn:dc:src\n"
        b"version: 1.0.0  # current release\nschema:\n  - name: t\n    version: 1.0.0\n"
    )
    expected = source.replace(b"version: 1.0.0  #", b"version: 1.1.0  #")
    assert copy(source, version="1.1.0") == expected


def test_single_and_double_quoted_versions_keep_their_quotes() -> None:
    single = b"id: x\nversion: '1.0.0'\n"
    double = b'id: x\nversion: "1.0.0"\n'
    assert copy(single, version="1.1.0") == b"id: x\nversion: '1.1.0'\n"
    assert copy(double, version="1.1.0") == b'id: x\nversion: "1.1.0"\n'


def test_flow_style_yaml_root_changes_only_the_token() -> None:
    source = b"{apiVersion: v3.2.0, kind: DataContract, id: x, version: 1.0.0}\n"
    assert copy(source, version="1.1.0") == (
        b"{apiVersion: v3.2.0, kind: DataContract, id: x, version: 1.1.0}\n"
    )


def test_crlf_line_endings_and_a_leading_bom_are_preserved() -> None:
    source = BOM + b"id: x\r\nversion: 1.0.0\r\nname: y\r\n"
    assert copy(source, version="2.0.0") == BOM + b"id: x\r\nversion: 2.0.0\r\nname: y\r\n"


def test_non_ascii_and_emoji_before_the_key_do_not_shift_the_edit() -> None:
    source = "# héllo \U0001f600\nname: über \U0001f680\nversion: 1.0.0\nid: x\n".encode()
    assert copy(source, version="1.0.1") == (
        "# héllo \U0001f600\nname: über \U0001f680\nversion: 1.0.1\nid: x\n".encode()
    )


def test_tab_indented_json_changes_only_the_version_string_and_keeps_whitespace() -> None:
    source = b'{\n\t"id": "x",\n\t"version": "1.0.0",\n\t"schema": [{"version": "1.0.0"}]\n}\n'
    assert copy(source, version="1.1.0", mt=JSON) == (
        b'{\n\t"id": "x",\n\t"version": "1.1.0",\n\t"schema": [{"version": "1.0.0"}]\n}\n'
    )


def test_a_nested_version_key_is_never_touched() -> None:
    source = b"id: x\nschema:\n  version: 9.9.9\nversion: 1.0.0\n"
    assert copy(source, version="1.1.0") == b"id: x\nschema:\n  version: 9.9.9\nversion: 1.1.0\n"


def test_replacing_a_plain_yaml_id_emits_a_double_quoted_scalar() -> None:
    source = b"id: urn:dc:src\nversion: 1.0.0\n"
    result = copy(source, odcs_id="urn:dc:new")
    assert result == b'id: "urn:dc:new"\nversion: 1.0.0\n'
    assert load_yaml(result.decode()) == {"id": "urn:dc:new", "version": "1.0.0"}


def test_id_and_version_can_change_together() -> None:
    source = b"version: 1.0.0\nid: a\n"
    assert copy(source, odcs_id="b", version="1.1.0") == b'version: 1.1.0\nid: "b"\n'


@pytest.mark.parametrize(
    ("source", "field"),
    [
        (b"id: x\nversion: |-\n  1.0.0\n", "version"),
        (b"id: x\nversion: >-\n  1.0.0\n", "version"),
        (b"version: 1.0.0\nid: |-\n  x\n", "id"),
        (b"id: x\nversion: !!str 1.0.0\n", "version"),
        (b"id: x\nversion: a\n  b\n", "version"),
        (b"id: x\n", "version"),
        (b"id: x\nversion: [1, 0]\n", "version"),
    ],
)
def test_a_shape_that_cannot_be_edited_in_place_is_422_copy_field_not_editable(
    source: bytes, field: str
) -> None:
    kwargs = {"odcs_id": "y"} if field == "id" else {"version": "1.1.0"}
    error = problem(source, **kwargs)
    assert (error.status, error.slug) == (422, "copy-field-not-editable")
    assert field in (error.detail or "")


def test_a_json_source_with_a_non_string_version_is_refused() -> None:
    error = problem(b'{"id": "x", "version": 1}', version="1.1.0", mt=JSON)
    assert error.slug == "copy-field-not-editable"


def test_no_change_returns_the_input_bytes() -> None:
    source = b"id: x\nversion: |-\n  1.0.0\n"
    assert copy(source) == source
    assert copy(source, version="1.0.0", odcs_id="x") == source


def test_a_lone_surrogate_in_the_new_id_is_422_validation_error() -> None:
    error = problem(b"id: x\nversion: 1.0.0\n", odcs_id="a\ud800b")
    assert (error.status, error.slug) == (422, "validation-error")


_HOSTILE_ID = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Cs")), min_size=1, max_size=200
).filter(lambda value: value != "src")

BLOCK = "# c\napiVersion: v3.2.0\nkind: DataContract\nid: src # note\nversion: 1.0.0\n"
FLOW = "{apiVersion: v3.2.0, kind: DataContract, id: src, version: 1.0.0}\n"


@pytest.mark.parametrize("template", [BLOCK, FLOW])
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(new_id=_HOSTILE_ID)
def test_any_printable_id_replaces_only_the_id_token(template: str, new_id: str) -> None:
    source = template.encode()
    result = copy(source, odcs_id=new_id)
    expected = load_yaml(template)
    expected["id"] = new_id
    assert load_yaml(result.decode()) == expected
    start = template.index("id: src") + len("id: ")
    prefix, suffix = (
        source[: len(template[:start].encode())],
        source[len(template[: start + 3].encode()) :],
    )
    assert result.startswith(prefix)
    assert result.endswith(suffix)


def test_json_id_replacement_is_a_json_string() -> None:
    source = b'{"id": "src", "version": "1.0.0"}'
    result = copy(source, odcs_id='q"uo\\te', mt=JSON)
    assert load_json(result.decode()) == {"id": 'q"uo\\te', "version": "1.0.0"}
    assert json.loads(result)["id"] == 'q"uo\\te'


STARTER_KEYS = ["apiVersion", "kind", "id", "name", "version", "status"]


def test_starter_is_a_valid_yaml_document_with_the_six_starter_keys() -> None:
    raw, media_type = ADAPTER.starter(odcs_id="urn:dc:orders", name="Orders")
    assert media_type == YAML
    assert load_yaml(raw.decode()) == {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": "urn:dc:orders",
        "name": "Orders",
        "version": "0.1.0",
        "status": "draft",
    }
    assert isinstance(load_yaml(raw.decode())["version"], str)
    assert list(load_yaml(raw.decode())) == STARTER_KEYS


def test_the_starter_passes_the_d19_gate_with_the_requested_id() -> None:
    from dc_odcs import parse_contract

    raw, media_type = ADAPTER.starter(odcs_id="urn:dc:orders", name="Orders")
    parsed = parse_contract(raw, media_type)
    assert parsed.odcs_id == "urn:dc:orders"
    assert parsed.api_version == "v3.2.0"


def test_a_name_with_a_newline_a_colon_and_a_hash_stays_one_string() -> None:
    name = "Orders\nversion: 9.9.9 # x: y"
    raw, _ = ADAPTER.starter(odcs_id="urn:dc:orders", name=name)
    loaded = load_yaml(raw.decode())
    assert loaded["name"] == name
    assert loaded["version"] == "0.1.0"
    assert list(loaded) == STARTER_KEYS


def test_a_value_that_cannot_be_utf_8_encoded_is_refused_as_a_validation_error() -> None:
    with pytest.raises(ProblemError) as caught:
        ADAPTER.starter(odcs_id="urn:dc:orders", name="bad \ud800 surrogate")
    assert caught.value.status == 422


@settings(max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    name=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        min_size=1,
        max_size=200,
    ),
    odcs_id=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        min_size=1,
        max_size=200,
    ),
)
def test_any_name_and_id_round_trip_through_the_starter_without_adding_keys(
    name: str, odcs_id: str
) -> None:
    raw, _ = ADAPTER.starter(odcs_id=odcs_id, name=name)
    loaded = load_yaml(raw.decode())
    assert loaded["name"] == name
    assert loaded["id"] == odcs_id
    assert loaded["version"] == "0.1.0"
    assert list(loaded) == STARTER_KEYS
