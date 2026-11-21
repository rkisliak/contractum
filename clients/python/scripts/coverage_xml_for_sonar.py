"""Rewrite a coverage.py XML report so SonarQube can map its files from the repository root (D-48).

coverage.py writes file names relative to ``src`` or to the SDK directory, but the scanner resolves
report paths against its base directory, the repository root. This script prefixes every
``<class filename>`` with ``clients/python/``, points the single ``<source>`` at ``.`` and exits 1
listing every file that does not exist relative to the repository root, so a report Sonar could not
map never reaches the scan.

Usage: python scripts/coverage_xml_for_sonar.py coverage.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PREFIX = "clients/python/"


def repository_root() -> Path:
    """The repository root: this file lives in <root>/clients/python/scripts."""
    return Path(__file__).resolve().parents[3]


def rewrite(xml_path: Path, root: Path) -> list[str]:
    """Rewrite the report in place and return the filenames that do not resolve from ``root``."""
    tree = ET.parse(xml_path)  # noqa: S314 - the file was written by coverage in this same run
    report = tree.getroot()
    sources = report.find("sources")
    if sources is not None:
        for stale in list(sources):
            sources.remove(stale)
        ET.SubElement(sources, "source").text = "."
    missing: list[str] = []
    for node in report.iter("class"):
        filename = node.get("filename", "")
        if not filename.startswith(PREFIX):
            filename = PREFIX + filename
        node.set("filename", filename)
        if not (root / filename).is_file():
            missing.append(filename)
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    return missing


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: coverage_xml_for_sonar.py <coverage.xml>", file=sys.stderr)
        return 2
    missing = rewrite(Path(argv[1]), repository_root())
    if missing:
        print("coverage files that do not exist from the repository root:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
