"""Per-package Python coverage table and threshold gate (the full gate, ``CI_FULL=1``).

Reads the combined coverage data (``[tool.coverage.run] data_file`` in the root
``pyproject.toml``), groups files by workspace member (``identity``, ``contracts``, ...,
``shared/dc-core``) and prints statements, branches and the combined line+branch percentage that
coverage.py itself reports (covered lines plus covered branches over all lines plus branches).
Each package must reach its floor in ``[tool.dc-coverage.thresholds]``; raise a floor when
coverage rises, never lower it to make the gate pass.

    uv run python scripts/coverage_gate.py            table and gate
    uv run python scripts/coverage_gate.py --markdown table as Markdown (for coverage notes)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package_of(filename: str) -> str:
    rel = Path(filename).resolve().relative_to(ROOT).as_posix()
    return rel.split("/src/", 1)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--no-gate", action="store_true")
    args = parser.parse_args(argv)

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    thresholds: dict[str, float] = config["tool"]["dc-coverage"]["thresholds"]
    out = ROOT / ".coverage-data" / "coverage.json"
    subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "coverage", "json", "-q", "-o", str(out)],
        check=True,
        cwd=ROOT,
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for filename, info in data["files"].items():
        s = info["summary"]
        a = agg[package_of(filename)]
        a[0] += s["num_statements"]
        a[1] += s["covered_lines"]
        a[2] += s["num_branches"]
        a[3] += s["covered_branches"]

    rows = []
    failures = []
    for pkg in sorted(set(agg) | set(thresholds)):
        st, cl, br, cb = agg.get(pkg, [0, 0, 0, 0])
        lines = 100.0 * cl / st if st else 100.0
        branches = 100.0 * cb / br if br else 100.0
        combined = 100.0 * (cl + cb) / (st + br) if st + br else 100.0
        floor = thresholds.get(pkg)
        rows.append((pkg, st, lines, br, branches, combined, floor))
        if floor is None:
            failures.append(f"{pkg}: no threshold in [tool.dc-coverage.thresholds]")
        elif combined + 1e-9 < floor:
            failures.append(f"{pkg}: {combined:.2f}% < floor {floor}%")

    if args.markdown:
        print("| Package | Statements | Lines | Branches | Branch cov | Combined | Floor |")
        print("|---|---|---|---|---|---|---|")
        for pkg, st, lines, br, branches, combined, floor in rows:
            print(
                f"| {pkg} | {st} | {lines:.1f}% | {br} | {branches:.1f}% | {combined:.1f}% "
                f"| {floor}% |"
            )
    else:
        header = f"{'package':22} {'stmts':>6} {'lines':>7} {'branch':>6} {'br%':>7} {'total':>7}"
        print(f"{header} floor")
        for pkg, st, lines, br, branches, combined, floor in rows:
            print(
                f"{pkg:22} {st:6d} {lines:6.1f}% {br:6d} {branches:6.1f}% {combined:6.1f}% {floor}"
            )
    if failures and not args.no_gate:
        print("\ncoverage below floor:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
