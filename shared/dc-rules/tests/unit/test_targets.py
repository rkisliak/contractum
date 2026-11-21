"""Strict RFC 9535 targeting: removed regex filters, zero matches, lazy cap, deep documents.

`match()` and `search()` run on Python `re` and backtrack catastrophically (117 s measured), so
they are removed from the environment (D-137, T-5-01, T-5-57). Authors use CEL `matches()`.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from dc_rules.ports import RuleCompileError, Targeter, TooManyMatches
from dc_rules.targets import TargetError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "path",
    ["$.a[?search(@.n, 'x')]", "$.a[?match(@.n, 'x')]"],
    ids=["search", "match"],
)
def test_the_regex_filter_functions_fail_to_compile(targeter: Targeter, path: str) -> None:
    with pytest.raises(RuleCompileError) as caught:
        targeter.compile(path)
    assert caught.value.code == "target_invalid"


def test_a_malformed_path_fails_to_compile_as_target_invalid(targeter: Targeter) -> None:
    with pytest.raises(RuleCompileError) as caught:
        targeter.compile("$.a[")
    assert caught.value.code == "target_invalid"


def test_the_length_filter_function_still_works(targeter: Targeter) -> None:
    compiled = targeter.compile("$.a[?length(@.n) > 1]")
    document = {"a": [{"n": "xx"}, {"n": "y"}]}
    (found,) = targeter.select(compiled, document, 10)
    assert found.pointer == "/a/0"


@pytest.mark.parametrize("path", ["$.nothing", "$.schema[999]", "$.schema[0].nothing[*]"])
def test_zero_matches_is_not_an_error(targeter: Targeter, path: str) -> None:
    document: dict[str, Any] = {"schema": [{"name": "t"}]}
    assert list(targeter.select(targeter.compile(path), document, 10)) == []


def test_each_match_carries_a_json_pointer_and_a_normalised_path(targeter: Targeter) -> None:
    document = {"schema": [{"properties": [{"name": "id"}]}]}
    (found,) = targeter.select(targeter.compile("$.schema[*].properties[*]"), document, 10)
    assert found.pointer == "/schema/0/properties/0"
    assert found.path == "$['schema'][0]['properties'][0]"


def test_a_million_element_list_stops_lazily_at_the_cap(targeter: Targeter) -> None:
    document = {"xs": list(range(1_000_000))}
    compiled = targeter.compile("$.xs[*]")
    started = time.perf_counter()
    with pytest.raises(TooManyMatches):
        list(targeter.select(compiled, document, 10_000))
    assert time.perf_counter() - started < 2.0


def test_exactly_the_cap_is_not_too_many(targeter: Targeter) -> None:
    document = {"xs": list(range(10))}
    assert len(list(targeter.select(targeter.compile("$.xs[*]"), document, 10))) == 10


def test_a_3000_deep_document_is_a_target_error_not_a_crash(targeter: Targeter) -> None:
    document: Any = {}
    node = document
    for _ in range(3_000):
        node["a"] = {}
        node = node["a"]
    with pytest.raises(TargetError):
        list(targeter.select(targeter.compile("$..*"), document, 10_000))
