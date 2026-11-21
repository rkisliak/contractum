"""Version text caps on validation's run request match what contracts sends (CR-02, WR-01).

Contracts publishes any strict SemVer (build metadata has no length limit), so a cap of 64 on the
baseline's ``semver`` or a link's ``pinned_version`` turned a 66-character CI version into a 422
for every later run of the contract, which contracts reports as a permanent 503. Both caps are now
the shared ``dc_odcs.MAX_VERSION_CHARS``.
"""

from __future__ import annotations

import pytest
from dc_odcs import MAX_VERSION_CHARS
from dc_validation.runs import ValidationBaselineIn
from pydantic import ValidationError

pytestmark = pytest.mark.unit

# A CI-stamped version: 66 characters, valid strict SemVer.
LONG_BUILD = "1.0.0+build.20261113.sha.0123456789abcdef0123456789abcdef012345678"


def baseline(**fields: object) -> dict[str, object]:
    return {"media_type": "application/yaml", "document_b64": "", **fields}


def test_the_long_build_version_is_66_characters() -> None:
    assert len(LONG_BUILD) == 66


def test_a_baseline_with_long_build_metadata_is_accepted() -> None:
    parsed = ValidationBaselineIn.model_validate(baseline(semver=LONG_BUILD))
    assert parsed.semver == LONG_BUILD


def test_a_baseline_without_semver_is_accepted_and_derived_later() -> None:
    assert ValidationBaselineIn.model_validate(baseline()).semver is None
    assert ValidationBaselineIn.model_validate(baseline(semver=None)).semver is None


def test_the_baseline_cap_is_the_shared_constant() -> None:
    at_cap = "1.0.0+" + "a" * (MAX_VERSION_CHARS - 6)
    assert ValidationBaselineIn.model_validate(baseline(semver=at_cap)).semver == at_cap
    with pytest.raises(ValidationError):
        ValidationBaselineIn.model_validate(baseline(semver=at_cap + "a"))
    with pytest.raises(ValidationError):
        ValidationBaselineIn.model_validate(baseline(semver="1.0.0 <b>"))


def test_a_link_fact_pinning_a_long_build_version_is_accepted() -> None:
    from dc_validation.facts import ValidationRefFactsIn

    url = f"https://dc.example/api/v1/contracts/contracts/x/versions/{LONG_BUILD}" + "1" * 18
    pin = url.rsplit("/", 1)[-1]
    assert len(pin) == 84
    fact = {"state": "published", "pinned_version": pin, "pinned_published": True}
    parsed = ValidationRefFactsIn.model_validate({"contracts": {url: fact}})
    assert parsed.contracts[url].pinned_version == pin


def test_the_link_pin_cap_is_the_shared_constant() -> None:
    from dc_validation.facts import ValidationLinkFactIn

    at_cap = "1" * MAX_VERSION_CHARS
    assert ValidationLinkFactIn(state="not_found", pinned_version=at_cap).pinned_version == at_cap
    with pytest.raises(ValidationError):
        ValidationLinkFactIn(state="not_found", pinned_version=at_cap + "1")
