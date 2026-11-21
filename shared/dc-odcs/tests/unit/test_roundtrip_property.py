"""CONT-06: parsing never changes document content (JSON and YAML round trips)."""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml
from dc_odcs import parse_contract
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.unit


def _document_strategy(text: st.SearchStrategy[str]) -> st.SearchStrategy[dict[str, Any]]:
    scalars = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**63), max_value=2**63),
        st.floats(allow_nan=False, allow_infinity=False),
        text,
    )
    values = st.recursive(
        scalars,
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(text, children, max_size=4),
        ),
        max_leaves=12,
    )

    @st.composite
    def documents(draw: st.DrawFn) -> dict[str, Any]:
        extra = draw(st.dictionaries(text, values, max_size=5))
        for reserved in ("apiVersion", "kind", "id"):
            extra.pop(reserved, None)
        document: dict[str, Any] = {
            "apiVersion": "v3.2.0",
            "kind": "DataContract",
            "id": draw(text.filter(bool).map(lambda value: value[:255])),
        }
        document.update(extra)
        return document

    return documents()


# Strings exclude surrogates (not encodable) and U+0000 (rejected by the gate, Pitfall 4).
_TEXT = st.text(alphabet=st.characters(exclude_categories=["Cs"], exclude_characters="\x00"))
# The YAML generator also excludes U+0085 (NEL): PyYAML's own emitter writes it raw inside a
# quoted scalar and stock safe_load folds it to a space, so safe_load(safe_dump(x)) != x
# even without this library. That asymmetry lives upstream, not in dc_odcs.
_YAML_TEXT = st.text(
    alphabet=st.characters(exclude_categories=["Cs"], exclude_characters="\x00\x85")
)

_JSON_DOCUMENTS = _document_strategy(_TEXT)
_YAML_DOCUMENTS = _document_strategy(_YAML_TEXT)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_JSON_DOCUMENTS)
def test_json_round_trip_preserves_the_document(document: dict[str, Any]) -> None:
    parsed = parse_contract(json.dumps(document).encode(), "application/json")
    assert parsed.document == document


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_YAML_DOCUMENTS)
def test_yaml_round_trip_preserves_the_document(document: dict[str, Any]) -> None:
    raw = yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode()
    parsed = parse_contract(raw, "application/yaml")
    assert parsed.document == document


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_JSON_DOCUMENTS)
def test_key_order_is_preserved(document: dict[str, Any]) -> None:
    parsed = parse_contract(json.dumps(document).encode(), "application/json")
    assert list(parsed.document) == list(document)
