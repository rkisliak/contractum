"""``rule_set`` query values of the on-demand route (plan 05-12; D-122, D-126)."""

from __future__ import annotations

import uuid

import pytest
from dc_core.problems import ProblemError
from dc_validation.api.runs import parse_adhoc

pytestmark = pytest.mark.unit

ID = uuid.UUID("0197a5c0-0000-7000-8000-000000000001")


def test_a_bare_id_means_latest_and_an_at_suffix_an_exact_version() -> None:
    parsed = parse_adhoc([str(ID), f"{ID}@3", f"{ID}@2147483647"])

    assert [(a.rule_set_id, a.version) for a in parsed] == [
        (ID, "latest"),
        (ID, 3),
        (ID, 2_147_483_647),
    ]


def test_no_values_is_no_attachments() -> None:
    assert parse_adhoc([]) == []


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x",
        f"{ID}@",
        f"{ID}@0",
        f"{ID}@-1",
        f"{ID}@1.5",
        f"{ID}@one",
        f"{ID}@1@2",
        f"{ID}@2147483648",
        f"{ID}@latest",
        "@3",
        "a" * 81,
        f"{ID}@\u0663",  # a non-ASCII decimal digit
        f"{{{ID}}}",
        f"urn:uuid:{ID}",
        ID.hex,
    ],
)
def test_a_malformed_value_is_a_422_validation_error_that_does_not_echo_it(value: str) -> None:
    with pytest.raises(ProblemError) as caught:
        parse_adhoc([value])

    problem = caught.value
    assert problem.status == 422
    assert problem.slug == "validation-error"
    assert value not in str(problem.detail or "") or value == ""
