"""JUnit XML: well formed whatever the data holds, and honest about every status."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from support.reports import all_statuses, check, make_report, sample

from contractum.report.junit import report_to_junit

HOSTILE = "a\x00b\x0bc\x0cd\x01e<&>\"'f\ud800g"


def _parse(data: bytes) -> ET.Element:
    return ET.fromstring(data)  # noqa: S314


def _cases(root: ET.Element) -> dict[str, ET.Element]:
    return {case.get("name") or "": case for case in root.iter("testcase")}


def test_counts_and_structure_match_the_report() -> None:
    report = all_statuses()

    root = _parse(report_to_junit(report))

    suite = root.find("testsuite")
    assert suite is not None
    assert suite.attrib["name"] == "orders"
    assert suite.attrib["tests"] == "5"
    assert suite.attrib["failures"] == "1"
    assert suite.attrib["errors"] == "1"
    assert suite.attrib["skipped"] == "1"
    assert suite.attrib["timestamp"] == "2026-11-21T09:30:00+00:00"
    assert suite.attrib["time"] == "1.250"
    assert root.attrib["tests"] == "5"
    assert len(suite.findall("testcase")) == 5


def test_each_status_maps_to_its_junit_element() -> None:
    cases = _cases(_parse(report_to_junit(all_statuses())))

    assert [child.tag for child in cases["orders.id.present"]] == []
    failure = cases["orders.id.required"].find("failure")
    assert failure is not None
    assert failure.get("message") == "1 of 4 values are null"
    assert "row 1: None" in (failure.text or "")
    assert cases["orders.type"].find("error") is not None
    skipped = cases["orders.q[0]"].find("skipped")
    assert skipped is not None
    assert skipped.get("message") == "sql rules are not run"
    warning = cases["orders.amount.range"]
    assert warning.find("failure") is None
    assert "close to the limit" in (warning.findtext("system-out") or "")


def test_hostile_characters_are_stripped_and_markup_is_escaped() -> None:
    report = make_report(
        check(
            HOSTILE,
            "failed",
            column=HOSTILE,
            message=HOSTILE,
            samples=(sample(0, HOSTILE), sample(1, "x\x00y")),
        )
    )

    data = report_to_junit(report)
    root = _parse(data)

    case = next(root.iter("testcase"))
    failure = case.find("failure")
    assert failure is not None
    assert "<&>\"'" in (failure.get("message") or "")
    assert "<&>\"'" in (failure.text or "")
    assert "\x00" not in (failure.get("message") or "")
    assert b"&lt;&amp;&gt;" in data
    assert "row 1: xy" in (failure.text or "")


def test_a_markup_value_cannot_add_elements() -> None:
    report = make_report(
        check("orders.id", "failed", message="</failure><testcase name='injected'/>")
    )

    root = _parse(report_to_junit(report))

    assert [case.get("name") for case in root.iter("testcase")] == ["orders.id"]


def test_a_report_without_checks_is_an_empty_suite() -> None:
    root = _parse(report_to_junit(make_report()))

    suite = root.find("testsuite")
    assert suite is not None
    assert suite.attrib["tests"] == "0"
    assert list(suite) == []
