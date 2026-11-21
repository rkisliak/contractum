"""Pure Accept negotiation, cursor codec and LIKE escaping (no database)."""

from __future__ import annotations

import uuid

import pytest
from dc_contracts.convert import negotiate, render
from dc_contracts.search import decode_cursor, encode_cursor, like_escape, reject_nul
from dc_core.problems import ProblemError
from dc_odcs import load_json, load_yaml

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("accept", "stored", "expected"),
    [
        (None, "application/yaml", "original"),
        ("", "application/json", "original"),
        ("*/*", "application/yaml", "original"),
        ("application/yaml", "text/yaml", "original"),
        ("text/yaml", "application/x-yaml", "original"),
        ("application/json", "application/json", "original"),
        ("application/json", "application/yaml", "json"),
        ("application/yaml", "application/json", "yaml"),
        ("application/x-yaml", "application/json", "yaml"),
        ("application/*", "application/json", "original"),
        ("application/json;q=0.5, application/yaml", "application/yaml", "original"),
        ("application/yaml;q=0.5, application/json", "application/yaml", "json"),
        ("application/json, */*;q=0", "application/yaml", "json"),
        ("text/html, */*;q=0.1", "application/json", "original"),
        ("APPLICATION/JSON", "application/yaml", "json"),
    ],
)
def test_negotiate_picks_the_representation(accept: str | None, stored: str, expected: str) -> None:
    assert negotiate(accept, stored) == expected


@pytest.mark.parametrize(
    ("accept", "stored"),
    [
        ("text/html", "application/yaml"),
        ("image/png, text/html", "application/json"),
        ("application/json;q=0", "application/json"),
        ("application/yaml;q=0", "application/yaml"),
        ("garbage", "application/yaml"),
        ("application/json;q=abc", "application/yaml"),
        ("application/json;q=2", "application/yaml"),
    ],
)
def test_negotiate_refuses_what_cannot_be_produced(accept: str, stored: str) -> None:
    with pytest.raises(ProblemError) as caught:
        negotiate(accept, stored)
    assert caught.value.status == 406


def test_render_round_trips_through_the_strict_loaders() -> None:
    raw = b"b: 1\na: [NO, yes]\nname: 'NO'\n"
    body, media = render(raw, "application/yaml", "json")
    assert media == "application/json"
    assert load_json(body.decode()) == {"b": 1, "a": ["NO", "yes"], "name": "NO"}
    assert list(load_json(body.decode())) == ["b", "a", "name"]  # key order is not sorted
    back, media = render(body, "application/json", "yaml")
    assert media == "application/yaml"
    assert load_yaml(back.decode()) == load_yaml(raw.decode())


def test_render_strips_a_byte_order_mark_and_keeps_non_ascii() -> None:
    body, _ = render("\ufeffname: café\n".encode(), "text/yaml", "json")
    assert load_json(body.decode()) == {"name": "café"}
    assert "café".encode() in body  # ensure_ascii=False


def test_like_escape_escapes_the_backslash_first() -> None:
    assert like_escape("100%_done\\x") == "100\\%\\_done\\\\x"
    assert like_escape("plain") == "plain"


def test_cursor_round_trips_and_only_the_canonical_form_decodes() -> None:
    value = uuid.uuid4()
    assert decode_cursor(encode_cursor(value)) == value
    assert "=" not in encode_cursor(value)
    for bad in ("not-a-cursor", "", "AAAA", encode_cursor(value) + "!!"):
        with pytest.raises(ProblemError) as caught:
            decode_cursor(bad)
        assert caught.value.status == 422


def test_nul_in_a_text_filter_is_a_422() -> None:
    reject_nul(q="fine", tags=["a", "b"], name=None)
    with pytest.raises(ProblemError) as caught:
        reject_nul(tags=["ok", "bad\x00"])
    assert caught.value.status == 422
