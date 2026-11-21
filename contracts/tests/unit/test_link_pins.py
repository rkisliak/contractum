"""A link's pinned version always fits validation's wire cap (WR-01).

``pinned_version_of`` reads the strict SemVer a link ends in. A pin longer than the shared
``dc_odcs.MAX_VERSION_CHARS`` would make validation refuse the whole run (a permanent 503), so it
is reported as no pin (unverifiable) instead; a long CI version below the cap is kept.
"""

from __future__ import annotations

import pytest
from dc_contracts.link_facts import pinned_version_of
from dc_odcs import MAX_VERSION_CHARS

pytestmark = pytest.mark.unit

BASE = "https://dc.example/api/v1/contracts/contracts/0190a0a0-0000-7000-8000-000000000000"
LONG_BUILD = "1.0.0+build.20261113.sha.0123456789abcdef0123456789abcdef012345678"


def test_a_long_build_metadata_pin_is_kept() -> None:
    assert len(LONG_BUILD) == 66
    assert pinned_version_of(f"{BASE}/versions/{LONG_BUILD}") == LONG_BUILD


def test_a_pin_at_the_cap_is_kept_and_one_above_it_is_no_pin() -> None:
    at_cap = "1.0.0+" + "a" * (MAX_VERSION_CHARS - 6)
    assert pinned_version_of(f"{BASE}/versions/{at_cap}") == at_cap
    assert pinned_version_of(f"{BASE}/versions/{at_cap}a") is None


def test_a_non_semver_pin_is_no_pin() -> None:
    assert pinned_version_of(f"{BASE}/versions/1.0") is None
    assert pinned_version_of(BASE) is None
