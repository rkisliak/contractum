"""Argon2id password hashing off the event loop (T-01-15).

Passwords are NFKC-normalised before hashing and verifying, so the same visible password always
verifies. argon2-cffi defaults are Argon2id; ``needs_rehash`` reports parameter drift.
"""

import asyncio
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# A constant throw-away hash so an unknown username costs the same as a wrong password.
_DUMMY_PASSWORD = "dc-identity-dummy-password-not-a-credential"  # noqa: S105


def normalize_password(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


class PasswordService:
    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher()
        self._dummy_hash: str | None = None

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, normalize_password(password))

    async def verify(self, password_hash: str, password: str) -> bool:
        try:
            return await asyncio.to_thread(
                self._hasher.verify, password_hash, normalize_password(password)
            )
        except (VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)

    async def dummy_verify(self) -> None:
        """Burn one Argon2 verification; the result is irrelevant."""
        if self._dummy_hash is None:
            self._dummy_hash = await asyncio.to_thread(self._hasher.hash, _DUMMY_PASSWORD)
        await self.verify(self._dummy_hash, "wrong-" + _DUMMY_PASSWORD)
