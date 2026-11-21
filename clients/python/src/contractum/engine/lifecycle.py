"""The lifecycle check: policy about a deprecated contract version as a report entry.

A deprecated version is not a data mismatch, but a policy (fail on deprecated, fail when the
sunset is near) must exit 1 and stay visible, so it is reported as the check ``contract.lifecycle``.
"""

from __future__ import annotations

from datetime import date

from contractum.engine.checks import CheckOutcome

LIFECYCLE_CHECK_ID = "contract.lifecycle"


def _parse_date(value: date | str | None) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return value


def _outcome(failed: bool, message: str) -> CheckOutcome:
    return CheckOutcome(
        check_id=LIFECYCLE_CHECK_ID,
        category="lifecycle",
        column=None,
        rule="lifecycle",
        status="failed" if failed else "warning",
        severity="error" if failed else "warning",
        message=message,
    )


def lifecycle_outcome(
    *,
    deprecated: bool,
    sunset_date: date | str | None,
    today: date,
    fail_on_deprecated: bool,
    sunset_within_days: int | None,
) -> CheckOutcome | None:
    """None for a live version; a warning for a deprecated one; a failure when policy says so."""
    if not deprecated:
        return None
    sunset = _parse_date(sunset_date)
    when = f"sunset {sunset.isoformat()}" if sunset else "no sunset date"
    if fail_on_deprecated:
        return _outcome(True, f"contract version is deprecated ({when}) and deprecated is refused")
    if sunset is not None and sunset_within_days is not None:
        days = (sunset - today).days
        if days <= sunset_within_days:
            return _outcome(
                True,
                f"contract version is deprecated and sunsets {sunset.isoformat()} ({days} days)",
            )
    return _outcome(False, f"contract version is deprecated ({when})")
