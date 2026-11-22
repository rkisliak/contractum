"""The one-time bootstrap secret is compared through Argon2id, exactly and for any length."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from dc_identity.passwords import PasswordService

pytestmark = pytest.mark.unit

# A low-entropy stand-in of the right length, so it never looks like a real credential.
VALUE = "bootstrap-" * 4 + "value"


@pytest.fixture
def passwords() -> PasswordService:
    # Cheap parameters keep the test fast; the comparison logic is the same.
    return PasswordService(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


async def test_the_exact_secret_matches(passwords: PasswordService) -> None:
    assert await passwords.matches_secret(VALUE, VALUE) is True


@pytest.mark.parametrize(
    "candidate",
    [VALUE[:-1] + "x", VALUE[:10], VALUE + "x", "", VALUE.upper(), " " + VALUE],
)
async def test_any_other_candidate_is_refused(passwords: PasswordService, candidate: str) -> None:
    assert await passwords.matches_secret(VALUE, candidate) is False


async def test_the_candidate_is_not_normalised(passwords: PasswordService) -> None:
    # A full-width variant normalises (NFKC) to the secret; the bootstrap secret must match exactly.
    fullwidth = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in VALUE)
    assert await passwords.matches_secret(VALUE, fullwidth) is False
