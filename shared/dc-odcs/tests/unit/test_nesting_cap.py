"""Over-deep YAML and JSON are refused in linear time with a fixed, value-free problem."""

from __future__ import annotations

import time

import pytest
from dc_odcs import (
    MAX_NESTING_DEPTH,
    ContractRejected,
    NestingTooDeepError,
    load_json,
    load_yaml,
    parse_contract,
)

pytestmark = pytest.mark.unit

ONE_MIB = 1024 * 1024
YAML = "application/yaml"
JSON = "application/json"


def _flow(depth: int) -> str:
    return "[" * depth + "]" * depth


def _block_sequences(depth: int) -> str:
    return "".join(f"{' ' * (2 * i)}-\n" for i in range(depth)) + f"{' ' * (2 * depth)}x\n"


def _inline_block_sequences(depth: int) -> str:
    return "- " * depth + "x\n"  # compact nested block sequence: linear size


def _block_mappings(depth: int) -> str:
    return "".join(f"{' ' * (2 * i)}k:\n" for i in range(depth)) + f"{' ' * (2 * depth)}v\n"


def _json_objects(depth: int) -> str:
    return '{"a":' * depth + "1" + "}" * depth


def _contract_with_nest(nest: str, fmt: str) -> bytes:
    if fmt == JSON:
        text = (
            '{"apiVersion":"v3.2.0","kind":"DataContract","id":"c-1","version":"1.0.0",'
            f'"customProperties":[{{"property":"deep","value":{nest}}}]}}'
        )
    else:
        text = (
            "apiVersion: v3.2.0\nkind: DataContract\nid: c-1\nversion: 1.0.0\n"
            f"customProperties:\n  - property: deep\n    value: {nest}\n"
        )
    return text.encode("utf-8")


@pytest.mark.parametrize(
    ("make", "fmt"),
    [
        (_flow, YAML),
        (_block_sequences, YAML),
        (_block_mappings, YAML),
        (_flow, JSON),
        (_json_objects, JSON),
    ],
)
def test_a_nest_over_the_cap_is_refused_fast_with_a_fixed_detail(make, fmt) -> None:  # type: ignore[no-untyped-def]
    raw = make(MAX_NESTING_DEPTH + 1).encode("utf-8")

    with pytest.raises(ContractRejected) as raised:
        parse_contract(raw, fmt, max_bytes=ONE_MIB)

    assert raised.value.status == 422
    assert raised.value.slug == "malformed-document"
    assert "nesting is too deep" in raised.value.detail
    assert str(MAX_NESTING_DEPTH) in raised.value.detail


@pytest.mark.parametrize(
    ("make", "fmt"),
    [(_flow, YAML), (_inline_block_sequences, YAML), (_json_objects, JSON), (_flow, JSON)],
)
def test_a_one_mib_deep_nest_is_refused_in_well_under_a_second(make, fmt) -> None:  # type: ignore[no-untyped-def]
    depth = ONE_MIB // 4
    raw = make(depth).encode("utf-8")[:ONE_MIB]

    started = time.perf_counter()
    with pytest.raises(ContractRejected) as raised:
        parse_contract(raw, fmt, max_bytes=ONE_MIB)
    elapsed = time.perf_counter() - started

    assert raised.value.status == 422
    assert raised.value.slug == "malformed-document"
    assert elapsed < 1.0


@pytest.mark.parametrize("fmt", [YAML, JSON])
def test_the_detail_never_echoes_the_document(fmt: str) -> None:
    secret = "s3cr3t-value"
    nest = ("[" * (MAX_NESTING_DEPTH + 5)) + f'"{secret}"' + ("]" * (MAX_NESTING_DEPTH + 5))
    with pytest.raises(ContractRejected) as raised:
        parse_contract(_contract_with_nest(nest, fmt), fmt, max_bytes=ONE_MIB)

    assert secret not in raised.value.detail
    assert raised.value.pointer is None


@pytest.mark.parametrize("fmt", [YAML, JSON])
def test_a_document_exactly_at_the_cap_still_parses(fmt: str) -> None:
    # root mapping (1) + customProperties list (2) + its mapping (3) + the value nest.
    nest = _flow(MAX_NESTING_DEPTH - 3)

    parsed = parse_contract(_contract_with_nest(nest, fmt), fmt, max_bytes=ONE_MIB)

    assert parsed.document["id"] == "c-1"


@pytest.mark.parametrize("fmt", [YAML, JSON])
def test_one_level_past_the_cap_is_refused(fmt: str) -> None:
    nest = _flow(MAX_NESTING_DEPTH - 2)  # one deeper than the exact-cap case

    with pytest.raises(ContractRejected) as raised:
        parse_contract(_contract_with_nest(nest, fmt), fmt, max_bytes=ONE_MIB)

    assert raised.value.slug == "malformed-document"


def test_brackets_inside_json_strings_and_yaml_scalars_do_not_count() -> None:
    brackets = "[" * 1000 + "{" * 1000
    assert load_json('{"k": "' + brackets + '", "e": "\\"[[[["}') == {
        "k": brackets,
        "e": '"[[[[',
    }
    assert load_yaml(f"k: '{brackets}'\nj: \"{brackets}\"\n") == {"k": brackets, "j": brackets}


def test_the_loaders_raise_a_value_error_subclass_directly() -> None:
    with pytest.raises(NestingTooDeepError):
        load_yaml(_flow(MAX_NESTING_DEPTH + 1))
    with pytest.raises(NestingTooDeepError):
        load_json(_flow(MAX_NESTING_DEPTH + 1))
    assert issubclass(NestingTooDeepError, ValueError)


def test_an_unterminated_json_string_is_left_to_the_parser() -> None:
    with pytest.raises(ValueError, match="Unterminated string"):
        load_json('{"a": "' + "\\" * 5000)


@pytest.mark.parametrize(
    "body",
    [
        "\\n" * (ONE_MIB // 4),  # many escapes before the closing quote
        "\\\"" * (ONE_MIB // 4),  # many escaped quotes
        "!" * (ONE_MIB // 2),  # a long plain run
        "\\\\" * (ONE_MIB // 4),  # escaped backslashes only
    ],
)
def test_the_string_scan_is_linear_on_hostile_string_bodies(body: str) -> None:
    started = time.perf_counter()
    terminated = load_json('["' + body + '"]')
    unterminated_started = time.perf_counter()
    with pytest.raises(ValueError):
        load_json('["' + body)
    finished = time.perf_counter()

    assert isinstance(terminated, list)
    assert len(terminated) == 1
    assert unterminated_started - started < 1.0
    assert finished - unterminated_started < 1.0


def test_escapes_inside_json_strings_keep_the_depth_count_exact() -> None:
    # An escaped quote does not end the string, an escaped backslash does not escape the quote.
    hidden = '"\\"' + "[" * (MAX_NESTING_DEPTH + 5) + '"'
    assert load_json("[" + hidden + "]") == ['"' + "[" * (MAX_NESTING_DEPTH + 5)]
    closed = '"\\\\"' + "[" * (MAX_NESTING_DEPTH + 1) + "]" * (MAX_NESTING_DEPTH + 1)
    with pytest.raises(NestingTooDeepError):
        load_json("[" + closed + "]")
