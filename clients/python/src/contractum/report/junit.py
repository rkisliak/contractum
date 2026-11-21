"""The JUnit XML of a report: one ``testsuite`` per tested object, one ``testcase`` per check.

The XML is only generated here, never parsed. Characters XML 1.0 cannot carry are removed and
``xml.etree.ElementTree`` escapes the markup characters, so a hostile cell value cannot break
the document or add elements to it.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # noqa: S405  (generation only, nothing is parsed)

from contractum.report.model import CheckResult, Report
from contractum.report.text import detail_lines, strip_xml_invalid


def _text(value: object) -> str:
    return strip_xml_invalid(str(value))


def _body(check: CheckResult) -> str:
    return _text("\n".join(detail_lines(check)))


def _message(check: CheckResult) -> str:
    return _text(check.message or check.skipped_reason or check.status)


def _add_case(suite: ET.Element, check: CheckResult) -> None:
    case = ET.SubElement(
        suite, "testcase", classname=_text(check.category), name=_text(check.id), time="0"
    )
    if check.status == "failed":
        ET.SubElement(case, "failure", message=_message(check), type="failed").text = _body(check)
    elif check.status == "error":
        ET.SubElement(case, "error", message=_message(check), type="error").text = _body(check)
    elif check.status == "skipped":
        ET.SubElement(case, "skipped", message=_message(check))
    elif check.status == "warning":
        ET.SubElement(case, "system-out").text = _body(check)


def report_to_junit(report: Report) -> bytes:
    """The report as UTF-8 JUnit XML: failed -> failure, error -> error, skipped -> skipped.

    A warning stays a passed test case with its message in ``system-out``.
    """
    summary = report.summary
    counts = {
        "tests": str(summary.total),
        "failures": str(summary.failed),
        "errors": str(summary.error),
        "skipped": str(summary.skipped),
    }
    suites = ET.Element("testsuites", {"name": "contractum", **counts})
    suite = ET.SubElement(
        suites,
        "testsuite",
        {
            "name": _text(report.object),
            **counts,
            "time": f"{report.duration_ms / 1000:.3f}",
            "timestamp": report.started_at.isoformat(timespec="seconds"),
        },
    )
    for check in report.checks:
        _add_case(suite, check)
    ET.indent(suites)
    document: bytes = ET.tostring(suites, encoding="utf-8", xml_declaration=True)
    return document
