"""NIST 800-63B password policy (D-04): length and a common-password list, no composition rules."""

from __future__ import annotations

from collections.abc import Sequence
from importlib import resources

import pytest
from dc_core.problems import ProblemError
from dc_identity.password_policy import PasswordRules, check_password

pytestmark = pytest.mark.unit


def _bundled() -> list[str]:
    text = resources.files("dc_identity.data").joinpath("common_passwords.txt").read_text("utf-8")
    return text.splitlines()


def _entries() -> list[str]:
    return [line for line in _bundled() if line and not line.startswith("#")]


def _rejected(
    candidate: str, *, username: str = "admin", forbidden: Sequence[str] = ()
) -> ProblemError:
    with pytest.raises(ProblemError) as info:
        check_password(candidate, username=username, forbidden=forbidden)
    return info.value


def test_eleven_code_points_are_too_short_and_twelve_are_accepted() -> None:
    assert _rejected("k3j9x2m5q7w").status == 422
    check_password("k3j9x2m5q7wp", username="admin")


def test_the_upper_bound_is_128_code_points() -> None:
    check_password("k" * 128, username="admin")
    err = _rejected("k" * 129)
    assert err.status == 422
    assert err.slug == "password-policy"


def test_length_is_counted_after_nfkc() -> None:
    check_password("Å" * 12, username="admin")
    # 12 raw code points, but each "A + combining ring" composes to one under NFKC: 6 characters.
    assert _rejected("Å" * 6).status == 422


def test_a_bundled_entry_is_rejected_in_any_casing() -> None:
    entry = _entries()[len(_entries()) // 2]
    for candidate in (entry, entry.upper(), entry.title()):
        err = _rejected(candidate)
        assert err.status == 422
        assert "too common" in (err.detail or "")
        assert candidate not in (err.detail or "")


def test_the_username_is_rejected_after_casefolding() -> None:
    assert _rejected("Administrator-01", username="administrator-01").status == 422


def test_a_forbidden_value_is_rejected() -> None:
    err = _rejected("the-bootstrap-secret-value", forbidden=["The-Bootstrap-Secret-Value"])
    assert err.status == 422


def test_no_composition_rule_requires_digits_symbols_or_mixed_case() -> None:
    candidates = ("correct horse battery staple", "ALLUPPERCASEPASSPHRASE", "7391028465530917")
    common = set(_entries())
    for candidate in candidates:
        assert candidate.casefold() not in common, candidate
        check_password(candidate, username="admin")


def test_the_bundled_list_is_large_long_enough_and_cites_its_source() -> None:
    lines = _bundled()
    assert lines[0].startswith("# source:")
    assert "licence" in lines[0].lower()
    assert "http" in lines[0]
    entries = _entries()
    assert len(entries) >= 1000
    assert all(len(e) >= 12 for e in entries)
    assert entries == sorted(set(entries))
    assert all(e == e.casefold() for e in entries)


# --- character-class rules from the security settings (quick 261120-0ye, D-41) ------------


def _under(rules: PasswordRules, candidate: str) -> ProblemError:
    with pytest.raises(ProblemError) as info:
        check_password(candidate, username="admin", rules=rules)
    return info.value


def test_the_default_rules_are_todays_policy() -> None:
    assert PasswordRules.default() == PasswordRules(12, False, False, False, False)
    assert PasswordRules.default().describe() == ["At least 12 characters"]
    check_password("k3j9x2m5q7wp", username="admin", rules=PasswordRules.default())


def test_a_raised_minimum_length_applies() -> None:
    rules = PasswordRules(min_length=16)
    assert "16 to 128" in _under(rules, "k3j9x2m5q7wpabc").detail
    check_password("k3j9x2m5q7wpabcd", username="admin", rules=rules)


@pytest.mark.parametrize(
    ("rules", "bad", "good", "fragment"),
    [
        (
            PasswordRules(require_upper=True),
            "lowercase-only-pw1",
            "Lowercase-only-pw1",
            "upper-case",
        ),
        (
            PasswordRules(require_lower=True),
            "UPPERCASE-ONLY-PW1",
            "UPPERCASE-ONLY-PWd",
            "lower-case",
        ),
        (PasswordRules(require_digit=True), "NoDigitsHere-abc", "OneDigitHere-4bc", "digit"),
        (PasswordRules(require_symbol=True), "NoSymbolHere123", "Symbol-Here-123x", "symbol"),
    ],
)
def test_each_character_class_rule(
    rules: PasswordRules, bad: str, good: str, fragment: str
) -> None:
    err = _under(rules, bad)
    assert err.status == 422 and err.slug == "password-policy"
    assert fragment in err.detail
    assert bad not in err.detail
    check_password(good, username="admin", rules=rules)


def test_a_space_is_not_a_symbol_but_punctuation_and_unicode_symbols_are() -> None:
    rules = PasswordRules(require_symbol=True)
    assert "symbol" in _under(rules, "no symbols in this one 1").detail
    check_password("a symbol here: yes 1", username="admin", rules=rules)
    check_password("euro sign here € 11", username="admin", rules=rules)


def test_unicode_letters_count_as_upper_and_lower_case() -> None:
    rules = PasswordRules(require_upper=True, require_lower=True)
    check_password("Ünïcödé-pässwörd", username="admin", rules=rules)


def test_the_first_unmet_rule_is_named_in_a_fixed_order() -> None:
    rules = PasswordRules(
        min_length=20,
        require_upper=True,
        require_lower=True,
        require_digit=True,
        require_symbol=True,
    )
    assert "20 to 128" in _under(rules, "short").detail
    assert "upper-case" in _under(rules, "all-lowercase-and-1-long!").detail
    assert rules.describe() == [
        "At least 20 characters",
        "An upper-case letter",
        "A lower-case letter",
        "A digit",
        "A symbol",
    ]
