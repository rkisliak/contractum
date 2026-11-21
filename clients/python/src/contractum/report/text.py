"""Plain text shared by the report writers: the detail lines of one check and cell clean-up.

Only text the engine already rendered (masked and bounded samples) is used here; nothing is
read from the dataset again.
"""

from __future__ import annotations

import re

from contractum.report.model import CheckResult

# XML 1.0 forbids these code points, and a lone surrogate cannot be encoded as UTF-8.
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")

STATUS_WORDS = {
    "passed": "PASS",
    "failed": "FAIL",
    "warning": "WARN",
    "skipped": "SKIP",
    "error": "ERROR",
}


def strip_xml_invalid(text: str) -> str:
    """Remove the characters XML 1.0 cannot carry."""
    return _XML_INVALID.sub("", text)


def failing_text(check: CheckResult) -> str:
    """``n / total (p %)`` for a check with row counts, ``n`` with only a count, else ``-``."""
    if check.failed_rows is None:
        return "-"
    if not check.total_rows:
        return str(check.failed_rows)
    percent = check.failed_percent if check.failed_percent is not None else 0.0
    return f"{check.failed_rows} / {check.total_rows} ({percent:g} %)"


def sample_lines(check: CheckResult, limit: int | None = None) -> list[str]:
    """One ``row N: value`` line per sample, at most ``limit`` of them."""
    shown = check.samples if limit is None else check.samples[:limit]
    return [f"row {sample.row}: {sample.value}" for sample in shown]


def detail_lines(check: CheckResult, sample_limit: int | None = None) -> list[str]:
    """What a reader needs about one check: its message or skip reason, counts and samples."""
    lines: list[str] = []
    reason = check.message or check.skipped_reason
    if reason:
        lines.append(reason)
    if check.failed_rows is not None:
        lines.append(f"failing rows: {failing_text(check)}")
    if check.observed is not None:
        lines.append(f"observed: {check.observed}")
    if check.threshold is not None:
        lines.append(f"threshold: {check.threshold}")
    lines.extend(sample_lines(check, sample_limit))
    return lines
