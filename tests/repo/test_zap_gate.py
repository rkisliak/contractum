"""scripts/zap_gate.py: fails on unignored High ZAP alerts and refuses ignores without a reason."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "zap_gate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("zap_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["zap_gate"] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


zg = _load()


def _alert(plugin: str, risk: str, name: str = "An alert", count: int = 2) -> dict[str, Any]:
    return {
        "pluginid": plugin,
        "riskcode": risk,
        "name": name,
        "instances": [{"uri": "https://traefik/x", "evidence": "SECRET-BODY"}] * count,
    }


def _report(tmp_path: Path, name: str, *alerts: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"site": [{"@name": "https://traefik", "alerts": list(alerts)}]}))
    return path


def _rules(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "rules.tsv"
    path.write_text(text)
    return path


def test_an_unignored_high_alert_fails(tmp_path: Path) -> None:
    report = _report(tmp_path, "r.json", _alert("40018", "3", "SQL Injection"))
    rules = _rules(tmp_path, "# nothing ignored\n")
    assert zg.main([str(report), "--rules", str(rules)]) == 1


def test_an_ignored_high_alert_passes(tmp_path: Path) -> None:
    report = _report(tmp_path, "r.json", _alert("40018", "3", "SQL Injection"))
    rules = _rules(tmp_path, "40018\tIGNORE\t(false positive: the scanner reads a mock body)\n")
    assert zg.main([str(report), "--rules", str(rules)]) == 0


def test_medium_and_lower_alerts_pass(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        "r.json",
        _alert("10038", "2", "Content Security Policy Header Not Set"),
        _alert("10096", "1", "Timestamp Disclosure"),
        _alert("10027", "0", "Information Disclosure"),
    )
    rules = _rules(tmp_path, "")
    assert zg.main([str(report), "--rules", str(rules)]) == 0


def test_an_ignore_without_a_reason_is_refused(tmp_path: Path) -> None:
    report = _report(tmp_path, "r.json", _alert("40018", "3"))
    for row in ("40018\tIGNORE\n", "40018\tIGNORE\t\n", "40018\tIGNORE\t   \n"):
        rules = _rules(tmp_path, row)
        assert zg.main([str(report), "--rules", str(rules)]) == 2


def test_a_missing_report_is_an_error(tmp_path: Path) -> None:
    rules = _rules(tmp_path, "")
    assert zg.main([str(tmp_path / "absent.json"), "--rules", str(rules)]) == 2


def test_a_missing_rules_file_is_an_error(tmp_path: Path) -> None:
    report = _report(tmp_path, "r.json", _alert("40018", "2"))
    assert zg.main([str(report), "--rules", str(tmp_path / "absent.tsv")]) == 2


def test_an_unreadable_report_is_an_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert zg.main([str(bad), "--rules", str(_rules(tmp_path, ""))]) == 2


def test_a_high_alert_in_any_report_fails(tmp_path: Path) -> None:
    clean = _report(tmp_path, "a.json", _alert("10038", "2"))
    dirty = _report(tmp_path, "b.json", _alert("90020", "3", "Remote OS Command Injection"))
    assert zg.main([str(clean), str(dirty), "--rules", str(_rules(tmp_path, ""))]) == 1


def test_comments_and_other_actions_in_the_rules_file_are_tolerated(tmp_path: Path) -> None:
    report = _report(tmp_path, "r.json", _alert("40018", "3"))
    rules = _rules(
        tmp_path,
        "# header\n\n10038\tWARN\t(CSP is tracked elsewhere)\n10096\tFAIL\t(never allowed)\n",
    )
    assert zg.main([str(report), "--rules", str(rules)]) == 1


def test_the_summary_never_prints_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report(tmp_path, "r.json", _alert("40018", "3", "SQL Injection", count=3))
    zg.main([str(report), "--rules", str(_rules(tmp_path, ""))])
    out = capsys.readouterr().out
    assert "SQL Injection" in out
    assert "40018" in out
    assert "SECRET-BODY" not in out


RULES = Path(__file__).resolve().parents[2] / ".zap" / "rules.tsv"

# The 34 proven false-positive instances of the 2026-11-20 audit (run 1: 33 SQL injection
# instances, run 2: the path traversal instance), as (plugin, method, uri, param). No evidence.
PROVEN: list[tuple[str, str, str, str]] = [
    (
        "40018",
        "GET",
        "https://traefik/api/v1/contracts/contracts?folder_id=&include_subtree=false&odcs_id=&name=ZAP&domain=&status=&tags=&q=&limit=50&cursor=%27+AND+%271%27%3D%271%27+--+",
        "cursor",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/contracts/contracts?folder_id=&include_subtree=false&odcs_id=&name=ZAP&domain=&status=+AND+1%3D1+--+&tags=&q=&limit=50&cursor=",
        "status",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/contracts/folders/folder_id/cards?include_archived=false&cursor=%27+AND+%271%27%3D%271%27+--+&limit=50",
        "cursor",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/contracts/folders/folder_id/cards?include_archived=false&cursor=&limit=52-2",
        "limit",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/identity/entra/directory/search?kind=user&q=q&tenant_id=&owner_folder_id=%27+AND+%271%27%3D%271%27+--+",
        "owner_folder_id",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/identity/entra/groups?limit=50&cursor=%27+AND+%271%27%3D%271%27+--+",
        "cursor",
    ),
    ("40018", "GET", "https://traefik/api/v1/identity/entra/groups?limit=52-2&cursor=", "limit"),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/identity/entra/service-principals?limit=52-2&cursor=",
        "limit",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/identity/entra/users?limit=50&cursor=%27+AND+%271%27%3D%271%27+--+",
        "cursor",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/identity/folders?parent_id=&path=&include_archived=false&limit=50&cursor=%22+AND+%221%22%3D%221%22+--+",
        "cursor",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/identity/folders?parent_id=&path=%27+AND+%271%27%3D%271%27+--+&include_archived=false&limit=50&cursor=",
        "path",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/identity/groups?owner_folder_id=&limit=50&cursor=%27+AND+%271%27%3D%271%27+--+",
        "cursor",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/identity/pats?limit=50&cursor=+AND+1%3D1+--+",
        "cursor",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/identity/service-accounts?owner_folder_id=&limit=50&cursor=%27+AND+%271%27%3D%271%27+--+",
        "cursor",
    ),
    (
        "40018",
        "GET",
        "https://traefik/api/v1/identity/service-accounts?owner_folder_id=&limit=52-2&cursor=",
        "limit",
    ),
    ("40018", "POST", "https://traefik/api/v1/contracts/contracts/contract_id/drafts", "bump"),
    (
        "40018",
        "POST",
        "https://traefik/api/v1/contracts/contracts/contract_id/subscriptions/subscription_id/remove",
        "reason",
    ),
    (
        "40018",
        "POST",
        "https://traefik/api/v1/contracts/contracts/contract_id/versions/ref/deprecate",
        "reason",
    ),
    (
        "40018",
        "POST",
        "https://traefik/api/v1/contracts/contracts/contract_id/versions/ref/unpublish",
        "John Doe",
    ),
    (
        "40018",
        "POST",
        "https://traefik/api/v1/contracts/folders/folder_id/contracts/from-template",
        "odcs_id",
    ),
    (
        "40018",
        "POST",
        "https://traefik/api/v1/contracts/folders/folder_id/contracts/from-template",
        "template_id",
    ),
    (
        "40018",
        "POST",
        "https://traefik/api/v1/contracts/folders/folder_id/contracts/from-template",
        "template_version",
    ),
    (
        "40018",
        "POST",
        "https://traefik/api/v1/contracts/folders/folder_id/contracts/from-version",
        "odcs_id",
    ),
    (
        "40018",
        "POST",
        "https://traefik/api/v1/contracts/folders/folder_id/contracts/from-version",
        "source_version",
    ),
    (
        "40018",
        "POST",
        "https://traefik/api/v1/contracts/governance/rule-set-dry-runs",
        "rule_set_id",
    ),
    ("40018", "POST", "https://traefik/api/v1/contracts/governance/rule-set-dry-runs", "version"),
    ("40018", "POST", "https://traefik/api/v1/identity/folders", "display_name"),
    ("40018", "POST", "https://traefik/api/v1/identity/folders", "parent_id"),
    ("40018", "POST", "https://traefik/api/v1/identity/folders", "slug"),
    ("40018", "POST", "https://traefik/api/v1/identity/folders/folder_id/grants", "principal_id"),
    ("40018", "POST", "https://traefik/api/v1/identity/folders/folder_id/grants", "principal_type"),
    ("40018", "POST", "https://traefik/api/v1/identity/folders/folder_id/grants", "role"),
    ("40018", "POST", "https://traefik/api/v1/identity/principals/names", "ids[0]"),
    ("6", "POST", "https://traefik/api/v1/identity/principals/names", "ids[0]"),
]


def _instance_alert(
    plugin: str, instances: list[tuple[str, str, str]], risk: str = "3"
) -> dict[str, Any]:
    return {
        "pluginid": plugin,
        "riskcode": risk,
        "name": "An alert",
        "instances": [
            {"method": method, "uri": uri, "param": param, "evidence": "SECRET-BODY"}
            for method, uri, param in instances
        ],
    }


def _row(plugin: str, regex: str, params: str = "a", reason: str | None = "r") -> str:
    lines = []
    if reason is not None:
        lines.append(f"# reason: {reason}")
    if params is not None:
        lines.append(f"# params: {params}")
    lines.append(f"{plugin}\tOUTOFSCOPE\t{regex}")
    return "\n".join(lines) + "\n"


def _gate(tmp_path: Path, rules_text: str, *alerts: dict[str, Any]) -> int:
    report = _report(tmp_path, "r.json", *alerts)
    return int(zg.main([str(report), "--rules", str(_rules(tmp_path, rules_text))]))


COVERING = _row("40018", r"^https://traefik/api/v1/x/[^/?]+/y(\?.*)?$", "cursor, limit")
X_URI = "https://traefik/api/v1/x/abc/y?cursor=1"


def test_a_fully_covered_alert_is_out_of_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    alert = _instance_alert("40018", [("GET", X_URI, "cursor"), ("GET", X_URI, "limit")])
    assert _gate(tmp_path, COVERING, alert) == 0
    out = capsys.readouterr().out
    assert "out-of-scope" in out
    assert "SECRET-BODY" not in out
    assert "https://traefik/api" not in out


def test_an_extra_instance_on_another_url_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    alert = _instance_alert(
        "40018",
        [("GET", X_URI, "cursor"), ("GET", "https://traefik/api/v1/other", "cursor")],
    )
    assert _gate(tmp_path, COVERING, alert) == 1
    captured = capsys.readouterr()
    assert "1 not covered" in captured.out
    assert "https://traefik/api" not in captured.out + captured.err


def test_an_extra_parameter_on_a_listed_url_fails(tmp_path: Path) -> None:
    alert = _instance_alert("40018", [("GET", X_URI, "cursor"), ("GET", X_URI, "q")])
    assert _gate(tmp_path, COVERING, alert) == 1


def test_an_entry_for_one_plugin_never_covers_another(tmp_path: Path) -> None:
    alert = _instance_alert("6", [("GET", X_URI, "cursor")])
    assert _gate(tmp_path, COVERING, alert) == 1


def test_the_url_regex_is_anchored_like_re_match(tmp_path: Path) -> None:
    alert = _instance_alert("40018", [("GET", "https://traefik/api/v1/x/abc/y/more", "cursor")])
    assert _gate(tmp_path, COVERING, alert) == 1


def test_an_alert_group_without_instances_cannot_be_proven(tmp_path: Path) -> None:
    bare = {"pluginid": "40018", "riskcode": "3", "name": "SQL Injection", "count": "2"}
    assert _gate(tmp_path, COVERING, bare) == 1
    empty = {**bare, "instances": []}
    assert _gate(tmp_path, COVERING, empty) == 1
    # a whole-id IGNORE still lets such a group through
    assert _gate(tmp_path, "40018\tIGNORE\t(a documented false positive)\n", bare) == 0


def test_a_covered_medium_alert_is_not_counted(tmp_path: Path) -> None:
    alert = _instance_alert("10038", [("GET", X_URI, "")], risk="2")
    assert _gate(tmp_path, COVERING, alert) == 0


@pytest.mark.parametrize(
    "rules_text",
    [
        _row("40018", r"^https://traefik/x$", reason=None),
        "# reason: r\n40018\tOUTOFSCOPE\t^https://traefik/x$\n",
        _row("40018", r"^https://traefik/x$", params=""),
        _row("", r"^https://traefik/x$"),
        _row("*", r"^https://traefik/x$"),
        _row("40018", r"^https://traefik/(x$"),
        _row("40018", r"https://traefik/x$"),
        _row("40018", r"^https://traefik/x"),
        _row("40018", r"^http://traefik/x$"),
        "# reason: r\n\n# params: a\n40018\tOUTOFSCOPE\t^https://traefik/x$\n",
    ],
    ids=[
        "no-reason",
        "no-params",
        "empty-params",
        "empty-plugin",
        "wildcard-plugin",
        "bad-regex",
        "no-start-anchor",
        "no-end-anchor",
        "not-https",
        "reason-in-another-paragraph",
    ],
)
def test_malformed_out_of_scope_rows_exit_two(tmp_path: Path, rules_text: str) -> None:
    alert = _instance_alert("40018", [("GET", X_URI, "cursor")])
    assert _gate(tmp_path, rules_text, alert) == 2


def test_a_reason_covers_the_rows_after_it_in_its_paragraph(tmp_path: Path) -> None:
    rules_text = (
        "# reason: shared reason\n"
        "# params: cursor\n"
        "40018\tOUTOFSCOPE\t^https://traefik/api/v1/x/[^/?]+/y(\\?.*)?$\n"
        "# params: limit\n"
        "40018\tOUTOFSCOPE\t^https://traefik/api/v1/z(\\?.*)?$\n"
    )
    alert = _instance_alert(
        "40018", [("GET", X_URI, "cursor"), ("GET", "https://traefik/api/v1/z?a=1", "limit")]
    )
    assert _gate(tmp_path, rules_text, alert) == 0


def _committed_rule_lines() -> list[list[str]]:
    lines = RULES.read_text(encoding="utf-8").splitlines()
    return [ln.split("\t") for ln in lines if ln.strip() and not ln.startswith("#")]


def test_the_committed_rules_file_parses() -> None:
    rules = zg.load_rules(RULES)
    assert "40018" not in rules.ignored
    assert "6" not in rules.ignored
    assert rules.out_of_scope


def test_the_committed_file_splits_like_zaps_parser() -> None:
    for columns in _committed_rule_lines():
        assert len(columns) == 3, columns
        if columns[1] == "OUTOFSCOPE":
            re.compile(columns[2])


def test_the_committed_file_has_no_whole_id_ignore_for_sql_injection_or_path_traversal() -> None:
    for columns in _committed_rule_lines():
        if columns[1].strip() == "IGNORE":
            assert columns[0] not in {"40018", "6"}


def test_the_committed_rules_cover_all_34_proven_instances(tmp_path: Path) -> None:
    assert len(PROVEN) == 34
    for plugin in ("40018", "6"):
        instances = [(m, u, p) for pl, m, u, p in PROVEN if pl == plugin]
        assert (
            _gate(tmp_path, RULES.read_text(encoding="utf-8"), _instance_alert(plugin, instances))
            == 0
        )


SYNTHETIC_UNCOVERED = [
    ("40018", "GET", "https://traefik/api/v1/contracts/contracts?q=x", "q"),
    ("40018", "GET", "https://traefik/api/v1/contracts/contracts?q=x", "name"),
    ("40018", "POST", "https://traefik/api/v1/identity/principals/names", "ids[1]"),
    ("40018", "POST", "https://traefik/api/v1/identity/principals/names", "ids"),
    ("40018", "GET", "https://traefik/api/v1/identity/users?cursor=x", "cursor"),
    ("40018", "GET", "https://traefik/api/v1/identity/folders/abc/grants", "cursor"),
    ("40018", "GET", "https://evil.example/api/v1/identity/groups?cursor=x", "cursor"),
    ("6", "POST", "https://traefik/api/v1/identity/principals/names", "ids[1]"),
    ("6", "GET", "https://traefik/api/v1/identity/groups?cursor=x", "cursor"),
    ("6", "GET", "https://traefik/docs/..%2f..%2fetc/passwd", "file"),
]


@pytest.mark.parametrize(("plugin", "method", "uri", "param"), SYNTHETIC_UNCOVERED)
def test_the_committed_rules_fail_an_uncovered_instance(
    tmp_path: Path, plugin: str, method: str, uri: str, param: str
) -> None:
    covered = [(m, u, p) for pl, m, u, p in PROVEN if pl == plugin]
    alert = _instance_alert(plugin, [*covered, (method, uri, param)])
    assert _gate(tmp_path, RULES.read_text(encoding="utf-8"), alert) == 1
