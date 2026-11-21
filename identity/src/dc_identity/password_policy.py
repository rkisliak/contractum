"""Password policy (D-04, quick 261120-0ye D-41): length, a common-password list and, when a Global
admin turns them on, character-class rules.

The default is NIST 800-63B: length and the common-password list, no composition rules. The
stored security settings can raise the minimum length (never below ``MIN_LENGTH``) and require an
upper-case letter, a lower-case letter, a digit and a symbol. The rules apply wherever a *new*
password is set, never at login, so an existing password keeps working.

The bundled list (``data/common_passwords.txt``) holds only entries of at least ``MIN_LENGTH``
characters, NFKC-normalised and casefolded, because shorter ones can never pass the length rule.
Problem details name the first unmet rule and never echo the submitted password.
"""

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources

from dc_core.problems import ProblemError

MIN_LENGTH = 12
MAX_LENGTH = 128


@dataclass(frozen=True)
class PasswordRules:
    """The rules a new password must meet. ``default()`` is today's policy."""

    min_length: int = MIN_LENGTH
    require_upper: bool = False
    require_lower: bool = False
    require_digit: bool = False
    require_symbol: bool = False

    @classmethod
    def default(cls) -> "PasswordRules":
        return cls()

    def describe(self) -> list[str]:
        """The rules as short sentences, in a fixed order (the sign-in screens list these)."""
        lines = [f"At least {self.min_length} characters"]
        if self.require_upper:
            lines.append("An upper-case letter")
        if self.require_lower:
            lines.append("A lower-case letter")
        if self.require_digit:
            lines.append("A digit")
        if self.require_symbol:
            lines.append("A symbol")
        return lines


@cache
def _common_passwords() -> frozenset[str]:
    """Load the bundled list once (comment lines start with ``#``)."""
    text = resources.files("dc_identity.data").joinpath("common_passwords.txt").read_text("utf-8")
    return frozenset(line for line in text.splitlines() if line and not line.startswith("#"))


def _policy_error(detail: str) -> ProblemError:
    return ProblemError(422, "password-policy", "Unprocessable Content", detail)


def _is_symbol(ch: str) -> bool:
    """A symbol is any character that is not a letter, a digit or whitespace."""
    return not (ch.isalpha() or ch.isdigit() or ch.isspace())


def check_password(
    candidate: str,
    *,
    username: str,
    forbidden: Sequence[str] = (),
    rules: PasswordRules | None = None,
) -> None:
    rules = rules or PasswordRules.default()
    normalized = unicodedata.normalize("NFKC", candidate)
    if not rules.min_length <= len(normalized) <= MAX_LENGTH:
        raise _policy_error(
            f"The password must be {rules.min_length} to {MAX_LENGTH} characters long."
        )
    if rules.require_upper and not any(unicodedata.category(c) == "Lu" for c in normalized):
        raise _policy_error("The password must contain an upper-case letter.")
    if rules.require_lower and not any(unicodedata.category(c) == "Ll" for c in normalized):
        raise _policy_error("The password must contain a lower-case letter.")
    if rules.require_digit and not any(unicodedata.category(c) == "Nd" for c in normalized):
        raise _policy_error("The password must contain a digit.")
    if rules.require_symbol and not any(_is_symbol(c) for c in normalized):
        raise _policy_error("The password must contain a symbol.")
    folded = normalized.casefold()
    if folded == unicodedata.normalize("NFKC", username).casefold():
        raise _policy_error("The password must not be the same as the username.")
    for value in forbidden:
        if value and folded == unicodedata.normalize("NFKC", value).casefold():
            raise _policy_error("The password must not be a credential that is already in use.")
    if folded in _common_passwords():
        raise _policy_error("The password is too common; choose a less predictable one.")
