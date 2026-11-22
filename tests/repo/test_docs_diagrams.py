"""Docs diagrams are pre-rendered PNG images, never live Mermaid blocks.

Live Mermaid sized each box before the page font loaded and clipped labels in some browsers, so
every diagram has a source in docs/diagrams/<name>.mmd and a committed PNG rendered by
scripts/render-diagrams.sh, and the pages embed the PNG image.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SOURCES = DOCS / "diagrams"
IMAGES = DOCS / "images" / "diagrams"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
IMAGE_LINK = re.compile(r"!\[[^\]]+\]\(([^)]*images/diagrams/[^)]+\.png)\)")


def _pages() -> list[Path]:
    return sorted(p for p in DOCS.rglob("*.md") if "diagrams" not in p.parts)


def test_no_live_mermaid_blocks() -> None:
    hits = [
        str(p.relative_to(ROOT))
        for p in [*_pages(), ROOT / "README.md"]
        if "```mermaid" in p.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_every_source_has_a_png_and_every_png_a_source() -> None:
    sources = {p.stem for p in SOURCES.glob("*.mmd")}
    images = {p.stem for p in IMAGES.glob("*.png")}
    assert sources, "no diagram sources found"
    assert sources == images
    for png in IMAGES.glob("*.png"):
        assert png.read_bytes()[:8] == PNG_MAGIC, png.name


def test_every_diagram_is_embedded_once_and_links_resolve() -> None:
    used: list[str] = []
    for page in _pages():
        for target in IMAGE_LINK.findall(page.read_text(encoding="utf-8")):
            resolved = (page.parent / target).resolve()
            assert resolved.is_file(), f"{page.relative_to(ROOT)}: {target}"
            used.append(resolved.stem)
    assert sorted(used) == sorted(p.stem for p in IMAGES.glob("*.png"))
