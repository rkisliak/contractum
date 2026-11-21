"""Gate on OWASP ZAP JSON reports: fail on any High alert that is not ignored (nightly DAST).

    python3 scripts/zap_gate.py REPORT.json [REPORT.json ...] --rules .zap/rules.tsv

Exit codes: 0 no unignored High alert, 1 at least one unignored High alert, 2 a usage or input
error (missing or unreadable report or rules file, or a malformed row in the rules file).

The rules file is the one ZAP itself reads (``-c``): tab separated columns, lines that start with
``#`` and blank lines are comments. Two row types matter here:

* ``<plugin id> TAB IGNORE TAB (<reason>)`` ignores a whole alert id. An IGNORE without a reason
  is refused: an ignore is only for a false positive, and a false positive has a reason.
* ``<plugin id> TAB OUTOFSCOPE TAB <URL regex>`` is ZAP's own per-URL exclusion (ZAP matches the
  whole third column with ``re.match`` against the alert URL, so nothing may follow the regex).
  ZAP cannot narrow by parameter, so two structured comments carry the rest: ``# reason: <text>``
  applies to every OUTOFSCOPE row after it in the same paragraph (a blank line ends a paragraph),
  and ``# params: a, b`` directly above a row lists the parameter names ZAP reports for it.
  The plugin column is exactly one concrete id (no empty value, no ``*``), and the regex must
  start with ``^https://`` and end with ``$``. A row that breaks this is an input error (exit 2).

The JSON report (``-J``) keeps every alert, whatever ZAP's ``-c`` file excludes, so this gate
re-applies the rows per alert instance and is the authoritative decision: a High group passes only
when every instance matches a row (plugin, URL regex and parameter). One instance outside the rows
blocks the group. A group that has no instance list cannot be proven and blocks unless its id has a
whole-id IGNORE.

Only the alert group (risk, plugin id, name, instance counts) is printed. Requests, responses,
URLs, parameter values, headers and evidence never reach the output, so a CI log cannot leak a
token.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HIGH = "3"
RISK_LABEL = {"3": "High", "2": "Medium", "1": "Low", "0": "Informational"}


class InputError(Exception):
    """A usage or input problem (exit code 2)."""


@dataclass(frozen=True)
class OutOfScope:
    """One OUTOFSCOPE row: the instances of ``plugin`` on a matching URL and listed parameter."""

    plugin: str
    pattern: re.Pattern[str]
    params: frozenset[str]
    reason: str
    line: int

    def covers(self, plugin: str, uri: str, param: str) -> bool:
        return (
            plugin == self.plugin and param in self.params and self.pattern.match(uri) is not None
        )


@dataclass(frozen=True)
class Rules:
    ignored: frozenset[str]
    out_of_scope: tuple[OutOfScope, ...]


_REASON = re.compile(r"^#\s*reason:\s*(.*)$")
_PARAMS = re.compile(r"^#\s*params:\s*(.*)$")


def load_rules(path: Path) -> Rules:
    """Parse IGNORE and OUTOFSCOPE rows; refuse a malformed row (exit 2 in ``main``)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read rules file {path}: {exc.strerror}") from exc
    ignored: set[str] = set()
    entries: list[OutOfScope] = []
    reason = ""
    params_above: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        where = f"{path}:{number}"
        if not line:
            reason, params_above = "", None
            continue
        if line.startswith("#"):
            found = _REASON.match(line)
            if found:
                reason = found.group(1).strip()
            found = _PARAMS.match(line)
            params_above = found.group(1) if found else None
            continue
        columns = raw.split("\t")
        plugin = columns[0].strip()
        action = columns[1].strip().upper() if len(columns) > 1 else ""
        third = columns[2] if len(columns) > 2 else ""
        declared, params_above = params_above, None
        if action == "IGNORE":
            if not third.strip().strip("()").strip():
                raise InputError(f"{where}: IGNORE for {plugin} has no reason")
            ignored.add(plugin)
        elif action == "OUTOFSCOPE":
            entries.append(_out_of_scope(where, number, plugin, columns, reason, declared))
    return Rules(frozenset(ignored), tuple(entries))


def _out_of_scope(
    where: str,
    number: int,
    plugin: str,
    columns: list[str],
    reason: str,
    declared: str | None,
) -> OutOfScope:
    if not plugin or "*" in plugin or any(ch.isspace() for ch in plugin):
        raise InputError(f"{where}: OUTOFSCOPE needs exactly one concrete plugin id")
    if len(columns) != 3:
        raise InputError(f"{where}: OUTOFSCOPE needs exactly three tab separated columns")
    regex = columns[2].rstrip("\r\n")
    if not (regex.startswith("^https://") and regex.endswith("$")):
        raise InputError(f"{where}: the URL regex must start with ^https:// and end with $")
    try:
        pattern = re.compile(regex)
    except re.error as exc:
        raise InputError(f"{where}: the URL regex does not compile: {exc}") from exc
    if not reason:
        raise InputError(f"{where}: OUTOFSCOPE for {plugin} has no '# reason:' in its paragraph")
    params = frozenset(p.strip() for p in (declared or "").split(",") if p.strip())
    if not params:
        raise InputError(f"{where}: OUTOFSCOPE for {plugin} needs a '# params:' line right above")
    return OutOfScope(plugin, pattern, params, reason, number)


def load_alerts(path: Path) -> list[dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read report {path}: {exc.strerror}") from exc
    except ValueError as exc:
        raise InputError(f"report {path} is not valid JSON") from exc
    sites = data.get("site") if isinstance(data, dict) else None
    if not isinstance(sites, list):
        raise InputError(f"report {path} has no site list")
    alerts: list[dict[str, object]] = []
    for site in sites:
        for alert in site.get("alerts", []) if isinstance(site, dict) else []:
            if isinstance(alert, dict):
                alerts.append(alert)
    return alerts


def _count(alert: dict[str, object]) -> int:
    instances = alert.get("instances")
    if isinstance(instances, list):
        return len(instances)
    try:
        return int(str(alert.get("count", 0)))
    except ValueError:
        return 0


def _uncovered(alert: dict[str, object], entries: tuple[OutOfScope, ...]) -> int | None:
    """Instances no row covers; None when the group has no usable instance list."""
    instances = alert.get("instances")
    if not isinstance(instances, list) or not instances:
        return None
    plugin = str(alert.get("pluginid", ""))
    count = 0
    for instance in instances:
        item = instance if isinstance(instance, dict) else {}
        uri = str(item.get("uri", ""))
        param = str(item.get("param", ""))
        if not any(entry.covers(plugin, uri, param) for entry in entries):
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("reports", nargs="+", type=Path, help="ZAP JSON report files")
    parser.add_argument("--rules", type=Path, required=True, help="the ZAP rules.tsv")
    args = parser.parse_args(argv)

    try:
        rules = load_rules(args.rules)
        reports = [(path, load_alerts(path)) for path in args.reports]
    except InputError as exc:
        print(f"zap_gate: {exc}", file=sys.stderr)
        return 2

    failing = 0
    for path, alerts in reports:
        print(f"{path.name}: {len(alerts)} alert group(s)")
        for alert in sorted(alerts, key=lambda a: -int(str(a.get("riskcode", 0)) or 0)):
            risk = str(alert.get("riskcode", ""))
            plugin = str(alert.get("pluginid", ""))
            name = str(alert.get("name", ""))
            label = RISK_LABEL.get(risk, risk)
            if plugin in rules.ignored:
                mark, detail = "ignored", ""
            elif risk != HIGH:
                mark, detail = "ok", ""
            else:
                uncovered = _uncovered(alert, rules.out_of_scope)
                if uncovered == 0:
                    mark, detail = "out-of-scope", ""
                else:
                    failing += 1
                    mark = "FAIL"
                    detail = "" if uncovered is None else f" ({uncovered} not covered)"
            print(f"  [{mark}] {label:<13} {plugin:>6}  {name}  x{_count(alert)}{detail}")
    if failing:
        print(f"zap_gate: {failing} unignored High alert group(s)", file=sys.stderr)
        return 1
    print("zap_gate: no unignored High alert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
