"""Unknown-property errors name the offending keys, never values (quick 261119-wk8)."""

from __future__ import annotations

import pytest
from dc_odcs import unknown_properties_message, validate_structure
from dc_odcs.validate import (
    MAX_NAMED_PROPERTIES,
    MAX_PROPERTY_NAME_CHARS,
    clean_property_name,
)

pytestmark = pytest.mark.unit

SENTENCE = "The object has properties the schema does not allow"


def _base() -> dict[str, object]:
    return {"apiVersion": "v3.2.0", "kind": "DataContract", "id": "c-1", "version": "1.0.0"}


def test_the_message_names_each_key_in_order() -> None:
    assert unknown_properties_message(["owner", "tags"]) == f"{SENTENCE}: `owner`, `tags`."


def test_without_names_the_sentence_is_unchanged() -> None:
    assert unknown_properties_message([]) == f"{SENTENCE}."


def test_more_than_five_names_are_summarised() -> None:
    names = [f"k{n}" for n in range(8)]
    message = unknown_properties_message(names, 8)
    assert message == f"{SENTENCE}: `k0`, `k1`, `k2`, `k3`, `k4` and 3 more."


def test_a_total_larger_than_the_names_given_still_counts_the_rest() -> None:
    assert unknown_properties_message(["a"], 4) == f"{SENTENCE}: `a` and 3 more."


def test_control_characters_and_backticks_never_reach_the_message() -> None:
    cleaned = clean_property_name("a\x00b\nc\r\td\u202ee`f\u2028g")
    assert cleaned == "abcde fg"
    assert all(ch.isprintable() for ch in cleaned)
    assert "`" not in cleaned
    assert "\n" not in cleaned


def test_a_long_name_is_cut_at_the_bound() -> None:
    cleaned = clean_property_name("x" * 500)
    assert len(cleaned) == MAX_PROPERTY_NAME_CHARS
    assert cleaned.endswith("...")


def test_a_top_level_unknown_key_is_named_on_the_report() -> None:
    report = validate_structure({**_base(), "owner": "someone", "tags2": 1}, "v3.2.0")

    (error,) = report.errors
    assert error.pointer == ""
    assert error.unexpected == ("owner", "tags2")
    assert error.unexpected_count == 2
    assert error.message == f"{SENTENCE}: `owner`, `tags2`."


def test_a_nested_unknown_key_keeps_its_pointer_and_never_shows_the_value() -> None:
    document = {**_base(), "schema": [{"name": "t", "bogus": "TOPSECRET-VALUE"}]}

    report = validate_structure(document, "v3.2.0")

    (error,) = report.errors
    assert error.pointer == "/schema/0"
    assert "`bogus`" in error.message
    assert "TOPSECRET" not in error.message


def test_many_unknown_keys_are_bounded_on_the_report() -> None:
    extra = {f"key{n}": n for n in range(40)}

    report = validate_structure({**_base(), **extra}, "v3.2.0")

    (error,) = report.errors
    assert len(error.unexpected) == MAX_NAMED_PROPERTIES
    assert error.unexpected_count == 40
    assert error.message.endswith("and 35 more.")


def test_other_messages_are_left_to_the_library() -> None:
    report = validate_structure({**_base(), "id": 5}, "v3.2.0")

    (error,) = report.errors
    assert error.unexpected == ()
    assert SENTENCE not in error.message
