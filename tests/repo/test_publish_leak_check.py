"""The manual public export and its leak check (scripts/publish/).

Loads ``scripts/publish/leak_check.py`` by path and plants markers in ``tmp_path`` trees. Marker
strings are assembled from pieces, so this file itself never carries a home path, an agent project
folder or a memory-file name. The owner address used here is on a reserved example domain.
"""

from __future__ import annotations

import importlib.util
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "publish" / "leak_check.py"
SCRIPT = ROOT / "scripts" / "publish" / "export-public.sh"
SECURITY_YML = ROOT / ".github" / "workflows" / "security.yml"
CLAUDE_MD = ROOT / ".claude" / "CLAUDE.md"

OWNER = "jane.owner@example.com"
HOME_MAC = "/" + "Users" + "/jane/src/app"
HOME_LINUX = "/" + "home" + "/jane/src/app"
AGENT_PROJECTS = ".claude" + "/projects/some-project"
MEMORY_FILE = "MEMORY" + ".md"
PLANNING = ".planning/STATE.md"


@pytest.fixture(scope="module")
def lc() -> ModuleType:
    spec = importlib.util.spec_from_file_location("leak_check_under_test", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: dataclasses look their module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write(tree: Path, rel: str, text: str) -> Path:
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def findings(lc: ModuleType, tree: Path, emails: tuple[str, ...] = ()) -> list[str]:
    return [str(f) for f in lc.scan_tree(tree, emails)]


@pytest.fixture
def clean_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    write(tree, "README.md", "# Demo\n\nNothing private here.\n")
    write(tree, "src/app.py", "print('hello')\n")
    write(tree, "LICENSE", "Copyright the authors.\n")
    return tree


def test_clean_tree_has_no_findings(lc: ModuleType, clean_tree: Path) -> None:
    assert findings(lc, clean_tree, (OWNER,)) == []
    assert lc.main(["scan", str(clean_tree), "--owner-email", OWNER]) == 0


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        (f"cd {HOME_MAC}", "home-path"),
        (f"cd {HOME_LINUX}", "home-path"),
        (f"file://{HOME_MAC}/x", "home-path"),
        (f'path = "{HOME_LINUX}"', "home-path"),
        (f"see {AGENT_PROJECTS}/notes", "claude-projects"),
        (f"read {MEMORY_FILE} first", "memory-file"),
        (f"details in {PLANNING}", "planning-path"),
        ("details in `.planning`", "planning-path"),
        (f"mail {OWNER.upper()}", "owner-email"),
    ],
)
def test_each_marker_is_reported_without_its_text(
    lc: ModuleType,
    clean_tree: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    marker: str,
) -> None:
    write(clean_tree, "notes/todo.txt", f"first line\n{text}\n")
    assert findings(lc, clean_tree, (OWNER,)) == [f"notes/todo.txt:2: {marker}"]
    assert lc.main(["scan", str(clean_tree), "--owner-email", OWNER]) == 1
    out = capsys.readouterr()
    assert f"notes/todo.txt:2: {marker}" in out.out
    assert text not in out.out + out.err


@pytest.mark.parametrize(
    "text", ["replanning", "the.planningx file", "a.planning step", "planning"]
)
def test_planning_inside_a_longer_word_is_not_a_path(
    lc: ModuleType, clean_tree: Path, text: str
) -> None:
    write(clean_tree, "notes.md", text + "\n")
    assert findings(lc, clean_tree) == []


@pytest.mark.parametrize(
    "text",
    ["import { HomePage } from '@/features/home/HomePage';", "see src/features/home/HomePage.tsx"],
)
def test_relative_folders_named_home_are_not_home_paths(
    lc: ModuleType, clean_tree: Path, text: str
) -> None:
    write(clean_tree, "app.ts", text + "\n")
    assert findings(lc, clean_tree) == []


def test_planning_path_passes_only_in_an_allowlisted_file(lc: ModuleType, clean_tree: Path) -> None:
    write(clean_tree, ".gitignore", ".planning/state.json\n")
    assert findings(lc, clean_tree) == []
    write(clean_tree, "docs/guide.md", "see .planning/state.json\n")
    assert findings(lc, clean_tree) == ["docs/guide.md:1: planning-path"]


def test_allowlist_covers_only_its_marker(lc: ModuleType, clean_tree: Path) -> None:
    write(clean_tree, ".gitignore", f".planning/\n{HOME_MAC}\n")
    assert findings(lc, clean_tree) == [".gitignore:2: home-path"]


def test_allowlist_is_planning_only_and_every_entry_has_a_reason(lc: ModuleType) -> None:
    assert lc.ALLOWLIST
    for path, marker, reason in lc.ALLOWLIST:
        assert marker == "planning-path", path
        assert not path.startswith("/") and ".." not in path
        assert len(reason.split()) >= 3, path


def test_owner_email_passes_in_license_and_notice_files_only(
    lc: ModuleType, clean_tree: Path
) -> None:
    write(clean_tree, "LICENSE", f"Required Notice: Copyright Jane ({OWNER})\n")
    write(clean_tree, "NOTICE", f"Jane <{OWNER}>\n")
    write(clean_tree, "clients/sdk/NOTICE", f"Jane <{OWNER}>\n")
    assert findings(lc, clean_tree, (OWNER,)) == []
    write(clean_tree, "README.md", f"Contact {OWNER}\n")
    assert findings(lc, clean_tree, (OWNER,)) == ["README.md:1: owner-email"]


def test_owner_email_is_learned_from_license(lc: ModuleType, clean_tree: Path) -> None:
    write(clean_tree, "LICENSE", f"Copyright Jane ({OWNER})\n")
    write(clean_tree, "README.md", f"Contact {OWNER}\n")
    assert findings(lc, clean_tree) == ["README.md:1: owner-email"]


@pytest.mark.parametrize(
    "rel",
    [
        ".planning/STATE.md",
        ".gsd/state.json",
        ".dev/admin_password",
        ".security-reports/zap.json",
        ".coverage-data/data",
        ".claude/worktrees/agent-1/file.txt",
        ".claude/settings.local.json",
        AGENT_PROJECTS + "/notes.txt",
        "ui/.claude/memory/notes.txt",
        "docs/" + MEMORY_FILE,
    ],
)
def test_excluded_paths_are_findings(lc: ModuleType, clean_tree: Path, rel: str) -> None:
    write(clean_tree, rel, "harmless\n")
    top = rel.split("/")
    # A folder is reported once, at the excluded folder; a file at its own path.
    reported = [f for f in findings(lc, clean_tree) if f.endswith(": excluded-path")]
    assert len(reported) == 1
    assert rel.startswith(reported[0].removesuffix(": excluded-path")), (top, reported)


def test_binary_files_and_git_metadata_are_skipped(lc: ModuleType, clean_tree: Path) -> None:
    (clean_tree / "logo.png").write_bytes(b"\x89PNG\x00\x00" + HOME_MAC.encode())
    write(clean_tree, ".git/config", f"[user]\n\temail = {OWNER}\n")
    assert findings(lc, clean_tree, (OWNER,)) == []


def test_scan_can_be_limited_to_listed_files(lc: ModuleType, clean_tree: Path) -> None:
    write(clean_tree, "a.txt", f"{HOME_MAC}\n")
    write(clean_tree, "b.txt", "fine\n")
    assert [str(f) for f in lc.scan_tree(clean_tree, (), paths=["b.txt"])] == []
    assert [str(f) for f in lc.scan_tree(clean_tree, (), paths=["a.txt"])] == ["a.txt:1: home-path"]


SAMPLE = """# Project

intro
<!-- GSD:skills-start source:skills/ -->
skills
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->

## GSD Workflow Enforcement

use the workflow
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->

## Developer Profile

profile
<!-- GSD:profile-end -->
"""


def test_strip_removes_exactly_the_workflow_and_profile_blocks(lc: ModuleType) -> None:
    assert lc.strip_claude_md(SAMPLE) == (
        "# Project\n\nintro\n<!-- GSD:skills-start source:skills/ -->\nskills\n"
        "<!-- GSD:skills-end -->\n"
    )


def test_strip_keeps_text_after_the_blocks(lc: ModuleType) -> None:
    text = SAMPLE + "\n## Later\n\nkept\n"
    stripped = lc.strip_claude_md(text)
    assert stripped.endswith("<!-- GSD:skills-end -->\n\n## Later\n\nkept\n")
    assert "GSD:workflow" not in stripped and "GSD:profile" not in stripped


@pytest.mark.parametrize(
    "missing",
    [
        "<!-- GSD:workflow-start source:GSD defaults -->\n",
        "<!-- GSD:workflow-end -->\n",
        "<!-- GSD:profile-start -->\n",
        "<!-- GSD:profile-end -->\n",
    ],
)
def test_strip_fails_loudly_when_a_marker_is_missing(
    lc: ModuleType, tmp_path: Path, missing: str
) -> None:
    text = SAMPLE.replace(missing, "")
    with pytest.raises(lc.StripError):
        lc.strip_claude_md(text)
    target = write(tmp_path, "CLAUDE.md", text)
    assert lc.main(["strip-claude-md", str(target)]) != 0
    assert target.read_text(encoding="utf-8") == text


def test_strip_on_the_real_claude_md(lc: ModuleType, tmp_path: Path) -> None:
    # The committed CLAUDE.md is the public one; the GSD workflow and profile blocks live only in a
    # local CLAUDE.local.md. Re-add them as the GSD tool writes them and check the strip removes
    # exactly those blocks and leaves the committed file byte for byte.
    public = CLAUDE_MD.read_text(encoding="utf-8")
    assert "GSD:workflow" not in public
    assert "GSD:profile" not in public
    local_blocks = (
        "\n<!-- GSD:workflow-start source:GSD defaults -->\n\n## GSD Workflow Enforcement\n\n"
        "Start work through a GSD command.\n<!-- GSD:workflow-end -->\n\n"
        "<!-- GSD:profile-start -->\n\n## Developer Profile\n\n> Not configured.\n"
        "<!-- GSD:profile-end -->\n"
    )
    target = write(tmp_path, "CLAUDE.md", public.rstrip("\n") + "\n" + local_blocks)
    assert lc.main(["strip-claude-md", str(target)]) == 0
    stripped = target.read_text(encoding="utf-8")
    assert "Technology Stack" in stripped
    for gone in ("GSD:workflow", "GSD:profile", "GSD Workflow Enforcement", "Developer Profile"):
        assert gone not in stripped
    assert stripped == public.rstrip("\n") + "\n"


def fake_gitleaks(tmp_path: Path, exit_code: int) -> tuple[Path, Path]:
    log = tmp_path / "gitleaks-args.txt"
    tool = tmp_path / "fake-gitleaks"
    tool.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\nexit {exit_code}\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool, log


@pytest.mark.parametrize(("exit_code", "expected"), [(0, 0), (1, 1)])
def test_gitleaks_result_gates_the_scan(
    lc: ModuleType, clean_tree: Path, tmp_path: Path, exit_code: int, expected: int
) -> None:
    write(clean_tree, ".gitleaks.toml", "title = 'demo'\n")
    tool, log = fake_gitleaks(tmp_path, exit_code)
    assert lc.main(["scan", str(clean_tree), "--gitleaks", str(tool)]) == expected
    assert log.read_text().split("\n")[:-1] == [
        "git",
        "--config",
        str(clean_tree / ".gitleaks.toml"),
        "--redact",
        "--no-banner",
        "--exit-code",
        "1",
        str(clean_tree),
    ]


def test_gitleaks_without_its_config_fails(
    lc: ModuleType, clean_tree: Path, tmp_path: Path
) -> None:
    tool, _ = fake_gitleaks(tmp_path, 0)
    assert lc.main(["scan", str(clean_tree), "--gitleaks", str(tool)]) == 1


def _env_value(text: str, name: str) -> str:
    match = re.search(rf'^\s*{name}[:=]\s*"([^"]+)"', text, re.MULTILINE)
    assert match, name
    return match.group(1)


def test_gitleaks_pin_matches_the_security_workflow() -> None:
    workflow = SECURITY_YML.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    assert _env_value(script, "GITLEAKS_VERSION") == _env_value(workflow, "GITLEAKS_VERSION")
    assert _env_value(script, "GITLEAKS_SHA256_LINUX_X64") == _env_value(
        workflow, "GITLEAKS_SHA256"
    )
    for name in ("LINUX_ARM64", "DARWIN_X64", "DARWIN_ARM64"):
        assert re.fullmatch(r"[0-9a-f]{64}", _env_value(script, f"GITLEAKS_SHA256_{name}"))


def _script_code_lines() -> list[str]:
    """Script lines outside comments and heredoc bodies (heredocs only print text)."""
    lines: list[str] = []
    terminator: str | None = None
    for raw in SCRIPT.read_text(encoding="utf-8").splitlines():
        if terminator is not None:
            if raw.strip() == terminator:
                terminator = None
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        heredoc = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", raw)
        if heredoc:
            terminator = heredoc.group(1)
        lines.append(stripped)
    assert terminator is None, "unterminated heredoc"
    return lines


def test_script_never_pushes_or_adds_a_remote() -> None:
    code = _script_code_lines()
    assert code
    for line in code:
        assert not re.search(r"\bpush\b", line), line
        assert not re.search(r"\bremote\b", line), line
    text = SCRIPT.read_text(encoding="utf-8")
    assert "remote add origin" in text and "push -u origin main" in text


def test_script_shape() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)  # noqa: S603, S607
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "git clone --no-local" in text
    assert "leak_check.py" in text and "strip-claude-md" in text
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", text)


def test_tracked_files_pass_the_marker_scan(lc: ModuleType) -> None:
    """The export would fail on these; catch them here first (the planning folder never ships)."""
    listed = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    tracked = [p for p in listed.split("\0") if p and not p.startswith(".planning/")]
    assert len(tracked) > 100
    assert [str(f) for f in lc.scan_tree(ROOT, (), paths=tracked)] == []
