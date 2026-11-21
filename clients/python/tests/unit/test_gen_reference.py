"""Structure of the generated CLI and SDK API reference pages (scripts/gen_reference.py).

The byte-for-byte comparison with the committed pages is the ``--check`` step in the sdk-lint CI
job (it runs on the locked click). These tests stay structural, because the newest-releases job
resolves newer click versions whose usage text may differ.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import click
import pytest

import contractum
from contractum.cli import cli

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_reference.py"


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_reference_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _members(page: str) -> list[str]:
    lines = page.splitlines()
    start = lines.index("      members:") + 1
    return [
        line.strip().removeprefix("- ") for line in lines[start:] if line.startswith("        - ")
    ]


def test_sdk_members_are_the_public_api(gen: ModuleType) -> None:
    page = gen.render_sdk_api(gen.public_names())
    members = _members(page)
    assert members == sorted(set(contractum.__all__) - {"__version__"})
    assert all(not name.startswith("_") for name in members)
    assert "_generated" not in page
    assert "::: contractum" in page
    assert "show_root_heading: true" in page


def test_sdk_page_header(gen: ModuleType) -> None:
    page = gen.render_sdk_api(["Client"])
    assert page.startswith("# SDK API reference\n")
    assert gen.GENERATED_NOTE in page
    assert page.endswith("\n") and not page.endswith("\n\n")


def test_every_command_is_a_heading(gen: ModuleType) -> None:
    page = gen.render_cli(cli)
    headings = {
        line.split("`")[1] for line in page.splitlines() if line.startswith("#") and "`" in line
    }
    ctx = click.Context(cli, info_name="contractum")
    names = cli.list_commands(ctx)
    assert names
    for name in names:
        assert f"contractum {name}" in headings
    config = cli.get_command(ctx, "config")
    assert isinstance(config, click.Group)
    sub = config.list_commands(click.Context(config, info_name="config", parent=ctx))
    assert sub
    for name in sub:
        assert f"contractum config {name}" in headings


def test_cli_page_tables_escape_pipes_once(gen: ModuleType) -> None:
    page = gen.render_cli(cli)
    assert page.startswith("# CLI reference\n")
    assert gen.GENERATED_NOTE in page
    assert "\\\\|" not in page
    assert page.endswith("\n") and not page.endswith("\n\n")


def test_render_pages_targets_both_reference_pages(gen: ModuleType) -> None:
    pages = gen.render_pages()
    assert set(pages) == {gen.CLI_PAGE, gen.SDK_API_PAGE}
    assert all(path.parent == gen.REFERENCE_DIR for path in pages)


def test_check_mode_detects_a_stale_page_without_writing(gen: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "cli.md"
    target.write_text("old\n", encoding="utf-8")
    assert gen.write_or_check({target: "new\n"}, check=True) == 1
    assert target.read_text(encoding="utf-8") == "old\n"
    missing = tmp_path / "missing.md"
    assert gen.write_or_check({missing: "new\n"}, check=True) == 1
    assert not missing.exists()


def test_check_mode_passes_on_equal_bytes(gen: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "sdk-api.md"
    assert gen.write_or_check({target: "same\n"}, check=False) == 0
    assert target.read_bytes() == b"same\n"
    assert gen.write_or_check({target: "same\n"}, check=True) == 0


def test_help_text_urls_become_code_spans(gen: ModuleType) -> None:
    paragraphs = gen._help_paragraphs(
        "Set it, for example: x set url https://contractum.example.com.\n\n"
        "\b\nSee `https://kept.example` and <https://auto.example>.\fhidden"
    )
    assert paragraphs == [
        "Set it, for example: x set url `https://contractum.example.com`.",
        "See `https://kept.example` and <https://auto.example>.",
    ]
