"""Fail-closed edges of the in-place copy (CONT-06, T-03-15): every doubt is a 422, never a rewrite.

``copy_with`` refuses bytes it cannot prove it edits faithfully. The span finders and the strict
re-load helper are exercised directly for shapes the strict loaders already refuse upstream, so the
in-place editor stays safe on its own if a caller ever skips that gate.
"""

from __future__ import annotations

import pytest
from dc_contracts.notation import OdcsAdapter, _json_spans, _strict_load, _yaml_spans
from dc_core.problems import ProblemError

pytestmark = pytest.mark.unit

ADAPTER = OdcsAdapter()


def assert_not_editable(exc: pytest.ExceptionInfo[ProblemError], field: str) -> None:
    assert (exc.value.status, exc.value.slug) == (422, "copy-field-not-editable")
    assert f"top-level {field} " in (exc.value.detail or "")


def test_an_unsupported_stored_media_type_is_a_programming_error() -> None:
    with pytest.raises(RuntimeError, match="unsupported media type 'text/plain'"):
        ADAPTER.copy_with(b"id: x\n", "text/plain", odcs_id=None, version="1.1.0")


@pytest.mark.parametrize(
    ("raw", "media_type"),
    [
        (b"id: \xff\xfe\nversion: 1.0.0\n", "application/yaml"),  # not UTF-8
        (b"id: [\n", "application/yaml"),  # does not parse
        (b'{"id": "x",', "application/json"),  # does not parse
        (b"- id: x\n", "application/yaml"),  # not a mapping
        (b'["x"]', "application/json"),  # not an object
    ],
)
def test_a_source_that_is_not_a_readable_mapping_is_refused(raw: bytes, media_type: str) -> None:
    with pytest.raises(ProblemError) as exc:
        ADAPTER.copy_with(raw, media_type, odcs_id=None, version="1.1.0")
    assert_not_editable(exc, "version")


def test_a_json_source_without_the_field_cannot_gain_it_in_place() -> None:
    with pytest.raises(ProblemError) as exc:
        ADAPTER.copy_with(b'{"id": "x"}', "application/json", odcs_id=None, version="1.1.0")
    assert_not_editable(exc, "version")


@pytest.mark.parametrize("text", ["id: [\n", "- id\n", "plain scalar\n"])
def test_yaml_spans_refuse_text_that_is_not_a_root_mapping(text: str) -> None:
    with pytest.raises(ProblemError) as exc:
        _yaml_spans(text, ["id"])
    assert_not_editable(exc, "id")


@pytest.mark.parametrize(
    "text",
    [
        '["id"]',  # not an object
        "",  # empty
        '{id: "x"}',  # member name not quoted
        '{"id" "x"}',  # no colon
        '{"id": "x"',  # unterminated
    ],
)
def test_json_spans_refuse_text_that_is_not_a_json_object(text: str) -> None:
    with pytest.raises(ProblemError) as exc:
        _json_spans(text, ["id"])
    assert_not_editable(exc, "id")


def test_json_spans_locate_the_first_string_value_of_each_field() -> None:
    text = '{ "name": "n" , "id": "x"}'
    assert _json_spans(text, ["id"]) == {"id": (22, 25, '"')}


def test_strict_load_answers_none_for_yaml_the_strict_loader_refuses() -> None:
    assert _strict_load("a: &x 1\nb: *x\n") is None
    assert _strict_load("a: 1\na: 2\n") is None
    assert _strict_load("a: [\n") is None
    assert _strict_load("a: 1\n") == {"a": 1}
