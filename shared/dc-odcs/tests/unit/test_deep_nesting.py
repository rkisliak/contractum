"""Deeply nested documents near the 1 MiB body cap never escape as a crash (05-14 open item).

The gate must answer every deep document with a structured ContractRejected (422/413) or a parsed
document; a RecursionError or any other exception reaching the caller would surface as a 500.
"""

from __future__ import annotations

import signal

import pytest
from dc_odcs import ContractRejected, parse_contract, validate_structure

pytestmark = pytest.mark.unit

ONE_MIB = 1024 * 1024


def _envelope_with_nested_custom_property(depth: int) -> bytes:
    inner = "{}" if depth == 0 else '{"n":' + ("[" * depth) + ("]" * depth) + "}"
    text = (
        '{"apiVersion":"v3.2.0","kind":"DataContract","id":"c-1","version":"1.0.0",'
        '"customProperties":[{"property":"deep","value":' + inner + "}]}"
    )
    return text.encode("utf-8")


def test_a_one_mib_json_nest_is_refused_as_malformed_not_a_crash() -> None:
    depth = ONE_MIB // 2  # "[" * depth + "]" * depth fits just under the 1 MiB cap
    raw = b"[" * depth + b"]" * depth

    with pytest.raises(ContractRejected) as raised:
        parse_contract(raw, "application/json", max_bytes=ONE_MIB)

    assert raised.value.status == 422
    assert raised.value.slug == "malformed-document"


@pytest.mark.parametrize("depth", [4000, 60000])
def test_a_deep_yaml_flow_nest_is_refused_within_ten_seconds(depth: int) -> None:
    """A deep YAML flow nest must be refused quickly (the parser was measured super-linear)."""
    raw = ("[" * depth + "]" * depth).encode("utf-8")

    def _alarm(_signum: int, _frame: object) -> None:
        raise TimeoutError(f"YAML parse of depth {depth} did not finish in 10 s")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, 10.0)
    try:
        with pytest.raises(ContractRejected) as raised:
            parse_contract(raw, "application/yaml", max_bytes=ONE_MIB)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert raised.value.status == 422


def test_an_oversized_deep_nest_is_refused_by_the_byte_cap_before_parsing() -> None:
    raw = b"[" * (ONE_MIB + 1) + b"]" * (ONE_MIB + 1)

    with pytest.raises(ContractRejected) as raised:
        parse_contract(raw, "application/json", max_bytes=ONE_MIB)

    assert raised.value.status == 413
    assert raised.value.slug == "payload-too-large"


@pytest.mark.parametrize("depth", [50, 400, 3000])
def test_a_deep_custom_property_either_parses_or_is_a_structured_rejection(depth: int) -> None:
    raw = _envelope_with_nested_custom_property(depth)
    try:
        parsed = parse_contract(raw, "application/json", max_bytes=ONE_MIB)
    except ContractRejected as rejected:
        assert rejected.status in (413, 422)
        return

    report = validate_structure(parsed.document, "v3.2.0")
    assert report.valid in (True, False)
    assert len(report.errors) <= 50
