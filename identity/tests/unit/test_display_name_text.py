"""``DisplayName``: the one name rule (quick 261120-lbz, D-01), 1-200 characters, trimmed."""

from __future__ import annotations

import pytest
from dc_identity.text import DisplayName
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.unit


class _Model(BaseModel):
    name: DisplayName


def test_surrounding_whitespace_is_stripped() -> None:
    assert _Model(name="  Ana Maria ").name == "Ana Maria"


def test_a_tab_at_the_end_is_refused_not_stripped() -> None:
    with pytest.raises(ValidationError):
        _Model(name="Ana Maria \t")


@pytest.mark.parametrize(
    "value",
    ["Ana Maria", "x" * 200, "Zoë", "Łukasz Żółć", "山田 太郎"],
)
def test_ordinary_names_are_accepted(value: str) -> None:
    assert _Model(name=value).name == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "x" * 201,
        "a\x00b",
        "a\tb",
        "a\nb",
        "a\rb",
        "a\x7fb",
        "a\u0085b",
        "a b",
        "a b",
        "a\u202eb",
        "a\u202ab",
        "a\u2066b",
        "a\u2069b",
        "Zo\u200dë",
        "Ana \U0001f600",
    ],
)
def test_blank_long_control_and_bidi_names_are_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        _Model(name=value)
