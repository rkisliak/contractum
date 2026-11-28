"""The docs gate (scripts/docs_gate.py): stale release claims, docs impact, Vale wiring and pins.

No Vale and no network: the Vale run itself is exercised by the hooks and the docs-gate CI job.
The end-to-end cases run the script's modes in a scratch git repository.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import re
import stat
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "docs_gate.py"
DOCS_WORKFLOW = ROOT / ".github" / "workflows" / "docs.yml"
HOOKS = (ROOT / ".githooks" / "pre-commit", ROOT / ".githooks" / "commit-msg")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = _load("docs_gate", SCRIPT)


def _run_git(*args: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv built by the tests
        ["git", *args],  # noqa: S607 - git from PATH, like the gate itself
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _git(*args: str, cwd: Path = ROOT) -> str:
    return _run_git(*args, cwd=cwd).stdout


def _tracked() -> list[str]:
    # --others: a file added in the same change counts before it is committed.
    return _git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()


# --- 1. The real tree --------------------------------------------------------------------------


def test_no_reader_doc_claims_that_no_release_exists() -> None:
    if not gate.release_tags():
        if _git("rev-parse", "--is-shallow-repository").strip() == "true":
            pytest.skip("shallow clone without tags: cannot tell whether a release exists")
        return  # a full clone without a release tag: "no release yet" is then true
    hits = gate.check_stale(gate.tracked_reader_docs(), gate.read_worktree)
    assert [str(hit) for hit in hits] == []


def test_reader_docs_are_the_docs_and_readmes_only() -> None:
    docs = gate.tracked_reader_docs()
    assert "README.md" in docs
    assert "clients/python/README.md" in docs
    assert "docs/user/sdk-cli.md" in docs
    assert all(path.startswith("docs/") or path.endswith("README.md") for path in docs)
    for path in (
        ".claude/skills/keep-docs-current/SKILL.md",
        ".claude/skills/x/README.md",
        "docs/dev/CLAUDE.md",
        ".planning/README.md",
        "ui/node_modules/pkg/README.md",
        "docs/images/logo.svg",
        "CONTRIBUTING.md",
    ):
        assert not gate.is_reader_doc(path), path
    assert gate.counts_as_docs("mkdocs.yml")


# --- 2 and 3. The phrase list ----------------------------------------------------------------

PATTERNS = gate.load_stale_patterns()


def _hits(text: str) -> list[tuple[int, str]]:
    return list(gate.find_phrases(text, PATTERNS))


@pytest.mark.parametrize(
    "text",
    [
        "No release has been published yet.",
        "**No platform release has been published yet**: until the first one, wait.",
        "Until the first release, install from a checkout.",
        "Before the first release, build it yourself.",
        "The dashboard is coming soon.",
        "This option arrives in a future release.",
        "The SDK has not been released.",
        "Once the first stable release ships, pin it.",
    ],
)
def test_the_incident_sentences_are_caught(text: str) -> None:
    assert _hits(text), text


def test_a_phrase_split_across_a_hard_wrap_is_caught_on_its_first_line() -> None:
    text = "# Install\n\nIntro line.\nUntil the first\n  release, install from a checkout.\n"
    assert _hits(text) == [(4, "Until the first release")]


def test_a_phrase_split_inside_a_blockquote_is_caught() -> None:
    assert _hits("> No release has\n> been published yet.\n") == [
        (1, "No release has been published")
    ]


@pytest.mark.parametrize(
    "text",
    [
        "The `latest` tag never points at a version that was not released.",
        "Contract semver: no stable release yet, prereleases only.",
        "The card is accepted as soon as it passes the cap.",
        "The promote stops when there is no release candidate.",
        "Without any stable tag, the first release is `1.0.0`.",
        "The newest draft before the first publish.",
        "Run `echo No release has been published yet` to test.",
        "```text\nNo release has been published yet.\n```",
        "~~~\nUntil the first release\n~~~",
        "<!-- No release has been published yet. -->",
    ],
)
def test_known_good_sentences_pass(text: str) -> None:
    assert _hits(text) == []


def test_list_items_and_table_rows_are_separate_paragraphs() -> None:
    # "no release" ends one item and "has been published" starts the next: not one sentence.
    assert _hits("- There is no release\n- has been published means done\n") == []
    assert _hits("| until the first |\n| release |\n") == []


def test_a_finding_names_the_newest_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "release_tags", lambda: ["v1.0.0", "v1.1.0-rc.1", "v1.1.0"])
    findings = gate.check_stale(["page.md"], lambda _path: "x\n\nNo release exists here.\n")
    assert [str(f) for f in findings] == [
        "page.md:3: [stale-claim] 'No release exists' but tag v1.1.0 exists"
    ]


def test_no_tag_means_no_stale_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "release_tags", lambda: [])
    assert gate.check_stale(["p.md"], lambda _path: "No release has been published yet.") == []


@pytest.mark.parametrize(
    ("tags", "newest"),
    [
        (["v1.0.0", "v1.1.0-rc.1"], "v1.1.0-rc.1"),
        (["v1.1.1-rc.2", "v1.1.1", "v1.1.1-rc.1"], "v1.1.1"),
        (["v1.1.1-rc.2", "v1.1.1-rc.10"], "v1.1.1-rc.10"),
        (["v2.0.0", "v10.0.0"], "v10.0.0"),
    ],
)
def test_newest_tag(tags: list[str], newest: str) -> None:
    assert gate.newest_tag(tags) == newest


def test_the_phrase_list_parser(tmp_path: Path) -> None:
    rule = tmp_path / "Rule.yml"
    rule.write_text(
        "extends: existence\ntokens:\n  # a comment\n  - '\\bit''s gone\\b'\n  - plain # note\n"
        "level: error\n",
        encoding="utf-8",
    )
    assert [p.pattern for p in gate.load_stale_patterns(rule)] == [r"\bit's gone\b", "plain"]
    rule.write_text('tokens:\n  - "\\bx"\n', encoding="utf-8")
    with pytest.raises(gate.SetupError, match="single quotes"):
        gate.load_stale_patterns(rule)
    rule.write_text("tokens:\nlevel: error\n", encoding="utf-8")
    with pytest.raises(gate.SetupError, match="no `tokens:`"):
        gate.load_stale_patterns(rule)
    rule.write_text("tokens:\n  - '(unclosed'\n", encoding="utf-8")
    with pytest.raises(gate.SetupError, match="not a valid regex"):
        gate.load_stale_patterns(rule)
    with pytest.raises(gate.SetupError, match="missing"):
        gate.load_stale_patterns(tmp_path / "absent.yml")


# --- 4. The trailer ----------------------------------------------------------------------------


def _message(trailer: str) -> str:
    return f"refactor(audit): split the retention job\n\nBody text.\n\n{trailer}\n"


@pytest.mark.parametrize(
    "trailer",
    [
        "Docs-Impact: none — internal refactor of the retention job, no behaviour change",
        "Docs-Impact: none – internal refactor of the retention job",
        "Docs-Impact: none - internal refactor of the retention job",
        "Docs-Impact: none -- internal refactor of the retention job",
        "docs-impact: None — test-only change, nothing a reader sees",
        "Signed-off-by: A <a@example.invalid>\nDocs-Impact: none — renames a private helper only",
    ],
)
def test_trailer_accepted(trailer: str) -> None:
    assert gate.trailer_verdict([_message(trailer)]) == (True, [])


@pytest.mark.parametrize(
    ("trailer", "why"),
    [
        ("Docs-Impact: none", "not of the form"),
        ("Docs-Impact: none —", "real reason"),
        ("Docs-Impact: none — n/a", "docs are due"),
        ("Docs-Impact: none — later", "docs are due"),
        ("Docs-Impact: none — TBD.", "docs are due"),
        ("Docs-Impact: none — docs in a follow-up", "docs are due"),
        ("Docs-Impact: none — will add docs next week", "docs are due"),
        ("Docs-Impact: none — tiny", "real reason"),
        ("Docs-Impact: yes, the API guide", "not of the form"),
    ],
)
def test_trailer_refused(trailer: str, why: str) -> None:
    accepted, refusals = gate.trailer_verdict([_message(trailer)])
    assert not accepted
    assert len(refusals) == 1 and why in refusals[0]


def test_trailer_must_be_in_the_last_paragraph() -> None:
    message = (
        "fix: x\n\nDocs-Impact: none — internal refactor, no behaviour change\n\n"
        "A closing paragraph that is not a trailer.\n"
    )
    assert gate.trailer_verdict([message]) == (False, [])


def test_trailer_in_a_crlf_pull_request_body_with_headings() -> None:
    body = "PR\r\n\r\n## Summary\r\n\r\nDocs-Impact: none — CI-only change, no reader sees it\r\n"
    assert gate.trailer_verdict(["fix: x\n", body])[0]


def test_clean_message_drops_comments_and_the_scissors_part() -> None:
    message = (
        "fix: x\n\nDocs-Impact: none — internal refactor only\n# Please enter the message\n"
        "# ------------------------ >8 ------------------------\ndiff --git a/x b/x\n"
    )
    assert gate.clean_message(message) == "fix: x\n\nDocs-Impact: none — internal refactor only\n"
    assert gate.trailer_verdict([gate.clean_message(message)])[0]
    assert gate.clean_message("## Heading\r\nText", strip_comments=False) == "## Heading\nText\n"


# --- 5. The surface classifier -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "surface"),
    [
        ("ui/src/features/x/Thing.tsx", "UI"),
        ("ui/src/routes/index.tsx", "UI"),
        ("clients/python/src/contractum/client.py", "SDK public API"),
        ("clients/python/src/contractum/__init__.py", "SDK public API"),
        ("clients/python/src/contractum/engine/plan.py", "SDK public API"),
        ("clients/python/src/contractum/cli/test.py", "CLI"),
        ("identity/src/dc_identity/api/tokens.py", "Identity API"),
        ("mail/src/dc_mail/settings.py", "Settings"),
        ("deploy/helm/contractum/templates/deployment.yaml", "Helm"),
        ("scripts/release/next_version.py", "Release"),
        ("gateway/dynamic/middlewares.yml", "Gateway"),
        (".vale/styles/Contractum/StaleRelease.yml", "The docs gate"),
        (".githooks/pre-commit", "The docs gate"),
    ],
)
def test_surface(path: str, surface: str) -> None:
    assert surface in [s.name for s in gate.surfaces_for(path)]


@pytest.mark.parametrize(
    "path",
    [
        "ui/src/features/x/Thing.test.tsx",
        "ui/src/features/x/hook.test.ts",
        "clients/python/src/contractum/_transport.py",
        "identity/src/dc_identity/api/tests/test_x.py",
        "identity/src/dc_identity/api/__pycache__/x.cpython-314.pyc",
        "identity/src/dc_identity/api/conftest.py",
        "identity/src/dc_identity/service.py",
        ".vale/styles/config/vocabularies/Contractum/accept.txt",
        "docs/user/sdk-cli.md",
    ],
)
def test_not_a_surface(path: str) -> None:
    assert gate.surfaces_for(path) == []


def _diff(*changes: str) -> str:
    return "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n" + "\n".join(changes) + "\n"


SHA_A = "a" * 40
SHA_B = "b" * 40


def test_a_uses_only_diff_of_release_yml_is_not_reader_visible() -> None:
    diff = _diff(
        f"-      - uses: actions/checkout@{SHA_A} # v7.0.0",
        f"+      - uses: actions/checkout@{SHA_B} # v7.0.1",
        "+      # a comment",
    )
    assert gate.is_pin_only(".github/workflows/release.yml", diff)
    run = _diff('-        run: echo "a"', '+        run: echo "b"')
    assert not gate.is_pin_only(".github/workflows/release.yml", run)
    unpinned = _diff("+      - uses: actions/checkout@v7")
    assert not gate.is_pin_only(".github/workflows/release.yml", unpinned)


def test_an_image_pin_diff_of_values_yaml_is_not_reader_visible() -> None:
    diff = _diff("-    tag: 18.5", "+    tag: 18.6", "-  digest: sha256:1", "+  digest: sha256:2")
    assert gate.is_pin_only("deploy/helm/contractum/values.yaml", diff)
    assert not gate.is_pin_only("deploy/helm/contractum/values.yaml", _diff("+  replicas: 2"))
    # Only the listed files have a pin rule.
    assert not gate.is_pin_only("deploy/helm/contractum/Chart.yaml", diff)


def test_check_impact() -> None:
    no_diff: dict[str, str] = {}

    def diff_for(path: str) -> str:
        return no_diff.get(path, _diff("+changed = True"))

    code = ["ui/src/features/x/Thing.tsx"]
    findings = gate.check_impact(code, ["feat(ui): x\n"], diff_for)
    assert [f.check for f in findings] == ["docs-impact"]
    assert "docs/user/ui-guide.md" in findings[0].message
    assert gate.check_impact([*code, "docs/user/ui-guide.md"], ["feat: x\n"], diff_for) == []
    assert gate.check_impact([*code, "mkdocs.yml"], ["feat: x\n"], diff_for) == []
    trailer = "feat: x\n\nDocs-Impact: none — only renames an internal CSS class"
    assert gate.check_impact(code, [trailer], diff_for) == []
    refused = gate.check_impact(code, ["feat: x\n\nDocs-Impact: none — later"], diff_for)
    assert [f.path for f in refused] == ["ui/src/features/x/Thing.tsx", "trailer"]
    assert gate.check_impact(["identity/src/dc_identity/service.py"], ["x\n"], diff_for) == []


def test_glob_to_regex() -> None:
    assert gate.glob_to_regex("a/**").fullmatch("a/b/c.py")
    assert gate.glob_to_regex("**/tests/**").fullmatch("tests/x.py")
    assert gate.glob_to_regex("**/tests/**").fullmatch("a/b/tests/x.py")
    assert not gate.glob_to_regex("*/src/*/settings.py").fullmatch("a/b/src/c/settings.py")
    assert gate.glob_to_regex("x/{a,b}/**").fullmatch("x/b/y")
    assert not gate.glob_to_regex("x/_[!_]*.py").fullmatch("x/__init__.py")
    assert gate.glob_to_regex("x/_[!_]*.py").fullmatch("x/_api.py")
    assert gate.glob_to_regex("f?.md").fullmatch("f1.md")


# --- 6. Every surface glob still points at real files ------------------------------------------


def test_every_surface_glob_matches_a_tracked_file() -> None:
    tracked = _tracked()
    dead = [
        f"{surface.name}: {pattern}"
        for surface in gate.SURFACES
        for pattern in surface.globs
        if not any(gate.glob_to_regex(pattern).fullmatch(path) for path in tracked)
    ]
    assert dead == []


# --- 7. The Vale rule and the docs-site test agree ----------------------------------------------


def test_internal_id_prefixes_agree_with_the_docs_site_test() -> None:
    rule = (ROOT / ".vale/styles/Contractum/InternalIds.yml").read_text(encoding="utf-8")
    match = re.search(r"\\b\(\?:((?:[A-Z0-9]+\|)+[A-Z0-9]+)\)-\\d\+", rule)
    assert match, "the requirement-id token is missing from InternalIds.yml"
    site = _load("test_docs_site_prefixes", ROOT / "tests" / "repo" / "test_docs_site.py")
    assert sorted(match.group(1).split("|")) == sorted(site.REQUIREMENT_PREFIXES)
    for prefix in ("MAIL", "SRC", "TREE"):
        assert prefix in site.REQUIREMENT_PREFIXES


# --- 8. Pins and wiring ------------------------------------------------------------------------


def test_vale_packages_are_pinned_by_sha256() -> None:
    packages = gate.vale_packages()
    assert {name for name, _, _ in packages} == {"Google", "write-good"}
    for _, url, digest in packages:
        assert url.startswith("https://github.com/vale-cli/")
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_an_unpinned_vale_package_is_refused(tmp_path: Path) -> None:
    ini = tmp_path / ".vale.ini"
    ini.write_text("Packages = https://github.com/vale-cli/x/releases/x.zip\n", encoding="utf-8")
    with pytest.raises(gate.SetupError, match="not pinned"):
        gate.vale_packages(ini)
    ini.write_text(
        f"Packages = https://github.com/a/A.zip#sha256:{'a' * 64}, \\\n"
        f"  https://github.com/b/B.zip#sha256:{'b' * 64}\n",
        encoding="utf-8",
    )
    assert [name for name, _, _ in gate.vale_packages(ini)] == ["A", "B"]


def test_downloads_are_https_from_github_only() -> None:
    for url in ("http://github.com/x.zip", "https://example.com/x.zip"):
        with pytest.raises(gate.SetupError, match="refusing"):
            gate._download(url)


def test_the_ci_job_pins_vale_like_the_script() -> None:
    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["docs-gate"]
    assert job["env"]["VALE_VERSION"] == gate.VALE_VERSION
    assert re.fullmatch(r"[0-9a-f]{64}", job["env"]["VALE_SHA256"])
    assert f"jdkato/vale:v{gate.VALE_VERSION}@sha256:" in gate.VALE_IMAGE
    steps = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "sha256sum -c" in steps
    assert "docs_gate.py --sync" in steps
    assert "docs_gate.py --range" in steps
    assert "${{" not in steps, "event data goes through env, never into the script"
    checkout = job["steps"][0]
    assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkout["uses"])
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 0}
    assert "edited" in workflow[True]["pull_request"]["types"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in job
    assert "docs-gate" in workflow["jobs"]["pages-deploy"]["needs"]


@pytest.mark.parametrize("hook", HOOKS, ids=lambda p: p.name)
def test_hooks_are_executable_and_call_the_gate(hook: Path) -> None:
    assert os.stat(hook).st_mode & stat.S_IXUSR
    staged = _git("ls-files", "-s", "--", hook.relative_to(ROOT).as_posix()).split()
    if staged:
        assert staged[0] == "100755"
    text = hook.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert "python3 scripts/docs_gate.py" in text
    assert "no-verify" not in text


def test_downloaded_vale_packages_are_git_ignored() -> None:
    ignored = _git(
        "check-ignore",
        "--no-index",
        "-v",
        ".vale/styles/Google/Headings.yml",
        ".vale/styles/write-good/Weasel.yml",
    )
    assert ignored.count("\n") == 2
    for kept in (
        ".vale/styles/Contractum/StaleRelease.yml",
        ".vale/styles/config/vocabularies/Contractum/accept.txt",
    ):
        result = _run_git("check-ignore", "--no-index", "-q", kept, check=False)
        assert result.returncode == 1, kept


# --- The modes, end to end in a scratch repository ----------------------------------------------


@pytest.fixture
def scratch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A git repository with the phrase list, a docs page and a UI file; the gate points at it."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {
        "GIT_AUTHOR_NAME": "Docs Gate Test",
        "GIT_AUTHOR_EMAIL": "docs-gate@example.invalid",
        "GIT_COMMITTER_NAME": "Docs Gate Test",
        "GIT_COMMITTER_EMAIL": "docs-gate@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    repo = tmp_path / "repo"
    (repo / ".vale/styles/Contractum").mkdir(parents=True)
    (repo / ".vale/styles/Contractum/StaleRelease.yml").write_bytes(
        (ROOT / gate.STALE_RULE).read_bytes()
    )
    (repo / "docs").mkdir()
    (repo / "docs/page.md").write_text("# Page\n\nInstall the wheel.\n", encoding="utf-8")
    (repo / "ui/src/features").mkdir(parents=True)
    (repo / "ui/src/features/Thing.tsx").write_text("export {}\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "chore: start", cwd=repo)
    monkeypatch.setattr(gate, "ROOT", repo)
    yield repo


def test_stale_only_mode(scratch_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    page = scratch_repo / "docs/page.md"
    page.write_text("# Page\n\nNo release has been published yet.\n", encoding="utf-8")
    _git("commit", "-qam", "docs: claim", cwd=scratch_repo)
    assert gate.main(["--stale-only"]) == gate.EXIT_OK  # no tag: the claim is true
    _git("tag", "v1.0.0", cwd=scratch_repo)
    assert gate.main(["--stale-only"]) == gate.EXIT_FINDING
    out = capsys.readouterr().out
    assert "docs/page.md:3: [stale-claim]" in out
    assert "v1.0.0" in out
    assert "How to pass" in out and "StaleRelease.yml" in out


def test_commit_msg_mode(scratch_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (scratch_repo / "ui/src/features/Thing.tsx").write_text("export const a = 1\n", "utf-8")
    _git("add", "ui", cwd=scratch_repo)
    message = scratch_repo / "MSG"
    message.write_text("feat(ui): add a\n", encoding="utf-8")
    assert gate.main(["--commit-msg", str(message)]) == gate.EXIT_FINDING
    assert "ui/src/features/Thing.tsx: [docs-impact] UI changed" in capsys.readouterr().out
    message.write_text("feat(ui): add a\n\nDocs-Impact: none — internal constant only\n", "utf-8")
    assert gate.main(["--commit-msg", str(message)]) == gate.EXIT_OK
    message.write_text("fixup! feat(ui): add a\n", encoding="utf-8")
    assert gate.main(["--commit-msg", str(message)]) == gate.EXIT_OK
    (scratch_repo / "docs/page.md").write_text("# Page\n\nThe new constant.\n", "utf-8")
    _git("add", "docs", cwd=scratch_repo)
    message.write_text("feat(ui): add a\n", encoding="utf-8")
    assert gate.main(["--commit-msg", str(message)]) == gate.EXIT_OK


def test_staged_mode_without_docs_needs_no_vale(
    scratch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_vale() -> list[str]:
        raise AssertionError("Vale must not run when no doc is staged")

    monkeypatch.setattr(gate, "vale_command", no_vale)
    (scratch_repo / "ui/src/features/Thing.tsx").write_text("export const b = 2\n", "utf-8")
    _git("add", "ui", cwd=scratch_repo)
    assert gate.main(["--staged"]) == gate.EXIT_OK


def test_staged_mode_reads_the_index_and_needs_synced_packages(
    scratch_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git("tag", "v1.0.0", cwd=scratch_repo)
    (scratch_repo / ".vale.ini").write_text(
        f"Packages = https://github.com/vale-cli/google/releases/Google.zip#sha256:{'a' * 64}\n",
        encoding="utf-8",
    )
    page = scratch_repo / "docs/page.md"
    page.write_text("# Page\n\nUntil the first release, wait.\n", encoding="utf-8")
    _git("add", "docs", cwd=scratch_repo)
    page.write_text("# Page\n\nFixed only in the working tree.\n", encoding="utf-8")
    # Vale needs the packages: a setup problem (exit 2), never a silent pass.
    assert gate.main(["--staged"]) == gate.EXIT_SETUP
    assert "--sync" in capsys.readouterr().err
    stale = gate.check_stale(["docs/page.md"], gate.read_index)
    assert [f.line for f in stale] == [3]


def test_range_mode_takes_the_trailer_from_the_pr_body(
    scratch_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "run_vale", lambda paths: {})
    base = _git("rev-parse", "HEAD", cwd=scratch_repo).strip()
    (scratch_repo / "ui/src/features/Thing.tsx").write_text("export const c = 3\n", "utf-8")
    _git("commit", "-qam", "feat(ui): c", cwd=scratch_repo)
    head = _git("rev-parse", "HEAD", cwd=scratch_repo).strip()
    assert gate.main(["--range", base, head]) == gate.EXIT_FINDING
    body = scratch_repo / "body.txt"
    body.write_text("Docs-Impact: none — renames an internal constant\r\n", encoding="utf-8")
    assert gate.main(["--range", base, head, "--pr-body-file", str(body)]) == gate.EXIT_OK
    body.write_text("## Summary\r\n\r\nWhy.\r\n\r\nDocs-Impact: none — renames a constant\r\n")
    assert gate.main(["--range", base, head, "--pr-body-file", str(body)]) == gate.EXIT_OK
    body.write_text("## Summary\r\n\r\nDocs-Impact: none — later\r\n", encoding="utf-8")
    assert gate.main(["--range", base, head, "--pr-body-file", str(body)]) == gate.EXIT_FINDING
    # An unknown base (a first push: the null SHA) falls back to the parent of head.
    assert gate.main(["--range", "0" * 40, head]) == gate.EXIT_FINDING
    assert "is unknown here" in capsys.readouterr().out


def test_range_mode_on_a_root_commit(scratch_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "run_vale", lambda paths: {})
    head = _git("rev-parse", "HEAD", cwd=scratch_repo).strip()
    # The first commit adds the UI file and a doc: docs changed, so it passes.
    assert gate.main(["--range", "", head]) == gate.EXIT_OK


def test_vale_alerts_split_into_blocking_errors_and_touched_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts = {
        "docs/a.md": [
            {"Line": 3, "Check": "Contractum.InternalIds", "Message": "id", "Severity": "error"},
            {"Line": 4, "Check": "Google.Will", "Message": "will", "Severity": "warning"},
            {"Line": 9, "Check": "Google.Will", "Message": "old", "Severity": "warning"},
        ]
    }
    monkeypatch.setattr(gate, "run_vale", lambda paths: alerts)
    errors, warnings, total = gate.check_vale(["docs/a.md"], lambda _path: {4})
    assert [str(e) for e in errors] == ["docs/a.md:3: [vale Contractum.InternalIds] id"]
    assert [w.line for w in warnings] == [4]
    assert total == 2


def test_added_lines() -> None:
    diff = "@@ -1,0 +2,3 @@\n+a\n+b\n+c\n@@ -10 +12 @@\n-x\n+y\n@@ -20,2 +23,0 @@\n-p\n-q\n"
    assert gate.added_lines(diff) == {2, 3, 4, 12}


def test_vale_is_found_only_at_the_pinned_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "vale"
    fake.write_text("#!/bin/sh\necho 'vale version 3.0.0'\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))  # no docker either
    with pytest.raises(gate.SetupError, match=r"found `vale version 3\.0\.0`"):
        gate.vale_command()
    fake.write_text(f"#!/bin/sh\necho 'vale version {gate.VALE_VERSION}'\n", encoding="utf-8")
    assert gate.vale_command() == [str(fake)]


def test_pr_body_file_needs_range(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        gate.main(["--all", "--pr-body-file", "x"])
    assert exc.value.code == 2
    assert "--pr-body-file needs --range" in capsys.readouterr().err


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def styles_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / gate.STYLES_DIR).mkdir(parents=True)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    return tmp_path


def _pin(root: Path, data: bytes) -> None:
    digest = hashlib.sha256(data).hexdigest()
    (root / ".vale.ini").write_text(
        f"Packages = https://github.com/vale-cli/pkg/releases/download/v1/Pkg.zip#sha256:{digest}\n",
        encoding="utf-8",
    )


def test_sync_unpacks_a_package_whose_digest_matches(
    styles_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _zip({"Pkg/": b"", "Pkg/Rule.yml": b"extends: existence\n"})
    _pin(styles_root, data)
    monkeypatch.setattr(gate, "_download", lambda url: data)
    with pytest.raises(gate.SetupError, match="not synced"):
        gate.check_packages_synced()
    (styles_root / gate.STYLES_DIR / "Pkg").mkdir()
    (styles_root / gate.STYLES_DIR / "Pkg" / "Stale.yml").write_text("old", encoding="utf-8")
    assert gate.main(["--sync"]) == gate.EXIT_OK
    package = styles_root / gate.STYLES_DIR / "Pkg"
    assert (package / "Rule.yml").read_bytes() == b"extends: existence\n"
    assert not (package / "Stale.yml").exists()
    gate.check_packages_synced()
    # A new pin in .vale.ini asks for a new sync.
    _pin(styles_root, data + b"changed")
    with pytest.raises(gate.SetupError, match="not synced"):
        gate.check_packages_synced()


def test_sync_refuses_a_changed_zip(styles_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(styles_root, _zip({"Pkg/Rule.yml": b"a"}))
    monkeypatch.setattr(gate, "_download", lambda url: _zip({"Pkg/Rule.yml": b"b"}))
    assert gate.main(["--sync"]) == gate.EXIT_SETUP
    assert not (styles_root / gate.STYLES_DIR / "Pkg").exists()


@pytest.mark.parametrize("member", ["../evil.yml", "/abs.yml", "Other/Rule.yml", "Pkg/../x"])
def test_sync_refuses_a_path_outside_the_package(
    styles_root: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    data = _zip({member: b"x"})
    _pin(styles_root, data)
    monkeypatch.setattr(gate, "_download", lambda url: data)
    with pytest.raises(gate.SetupError, match="unexpected path"):
        gate.sync_packages()
    assert sorted(p.name for p in (styles_root / gate.STYLES_DIR).iterdir()) == []


def test_html_comments_are_skipped_also_across_lines_and_paragraphs() -> None:
    def blanked(text: str) -> str:
        return "".join("\n" if char == "\n" else " " for char in text)

    for prefix, comment, suffix in [
        ("a ", "<!-- x\ny -->", " b"),
        ("a ", "<!-- x --!>", " b"),
        ("a ", "<!-- open\nrest", ""),
    ]:
        result = gate.blank_html_comments(prefix + comment + suffix)
        assert result == prefix + blanked(comment) + suffix
    pattern = [re.compile(r"no release", re.IGNORECASE)]
    text = "Intro.\n\n<!--\nNo release\n\nyet -->\n\nNo release here.\n"
    assert [line for line, _ in gate.find_phrases(text, pattern)] == [8]
