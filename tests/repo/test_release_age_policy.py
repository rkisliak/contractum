"""The UI supply-chain wait (minimum package age) is 14 days, apart from a dated, temporary dip.

``ui/pnpm-workspace.yaml`` (``minimumReleaseAge``, minutes) and ``ui/scripts/check-pin-age.mjs``
(``MIN_AGE_DAYS``) refuse package versions younger than the wait, against freshly hijacked
releases. On 2026-11-22 the wait was lowered to 5 days so a security fix (eslint 10.15.0,
typescript-eslint 8.73.0 for GHSA-g3hv-75wg-m249) could land; from 2026-11-30 both must be back
at 14 days, and this test fails until they are.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
RESTORE_ON = dt.date(2026, 11, 30)


def current_wait_days() -> tuple[float, int]:
    yaml = (UI / "pnpm-workspace.yaml").read_text(encoding="utf-8")
    script = (UI / "scripts" / "check-pin-age.mjs").read_text(encoding="utf-8")
    minutes = re.search(r"^minimumReleaseAge:\s*(\d+)", yaml, re.MULTILINE)
    days = re.search(r"^const MIN_AGE_DAYS = (\d+);", script, re.MULTILINE)
    assert minutes is not None and days is not None
    return int(minutes.group(1)) / (24 * 60), int(days.group(1))


def test_both_guards_use_the_same_wait() -> None:
    pnpm_days, script_days = current_wait_days()
    assert pnpm_days == script_days


def test_the_wait_is_14_days_again_from_the_restore_date() -> None:
    pnpm_days, _ = current_wait_days()
    if dt.datetime.now(dt.UTC).date() >= RESTORE_ON:
        assert pnpm_days >= 14, "restore minimumReleaseAge to 20160 and MIN_AGE_DAYS to 14"
    else:
        assert pnpm_days >= 5
