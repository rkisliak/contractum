"""The platform version rules of the Release workflow (scripts/release/*.py).

Pure functions first (commit parsing, bump rules, candidate numbering, promotion, grouping), then
both scripts end to end against a throwaway git repository.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "release"
REPO_URL = "https://github.com/acme/contractum"


def _load(name: str, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module while the file runs
    spec.loader.exec_module(module)
    return module


# Loaded under its own name first, so changelog.py's `from next_version import ...` reuses it.
nv = _load("next_version", "next_version")
cl = _load("changelog", "release_changelog")


def commit(subject: str, body: str = "", sha: str = "a" * 40) -> object:
    return nv.parse_commit(sha, subject, body)


def ver(text: str) -> object:
    version = nv.parse_tag(f"v{text}")
    assert version is not None, text
    return version


# --- commit parsing -------------------------------------------------------------------------------


def test_a_squash_subject_gives_type_scope_description_and_pull_request() -> None:
    parsed = commit("fix(ci): pace the ZAP scan (#33)")
    assert (parsed.type, parsed.scope, parsed.description) == ("fix", "ci", "pace the ZAP scan")
    assert parsed.pull_request == 33
    assert parsed.breaking is False


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("feat(api)!: drop the v0 routes", ""),
        ("refactor!: new layout", ""),
        ("feat(api): new routes", "Details.\n\nBREAKING CHANGE: the v0 routes are gone"),
        ("fix: x", "BREAKING-CHANGE: y"),
        ("Rework everything", "BREAKING CHANGE: no conventional subject, still breaking"),
    ],
)
def test_breaking_changes_are_the_bang_or_the_footer(subject: str, body: str) -> None:
    assert commit(subject, body).breaking is True
    assert nv.bump_of(commit(subject, body)) is nv.Bump.MAJOR


def test_a_breaking_change_mentioned_in_prose_is_not_a_footer() -> None:
    assert commit("fix: x", "This avoids a BREAKING CHANGE: later.").breaking is False


@pytest.mark.parametrize(
    "subject",
    ["Update nats-py to 2.17.0 (#27)", "Initial public release", "fix:no space", "(scope): x"],
)
def test_a_subject_that_is_not_a_conventional_commit_has_no_type(subject: str) -> None:
    parsed = commit(subject)
    assert parsed.type is None
    assert nv.bump_of(parsed) is nv.Bump.NONE


def test_type_and_scope_are_case_insensitive() -> None:
    parsed = commit("Feat(UI): Dark mode")
    assert (parsed.type, parsed.scope) == ("feat", "ui")


# --- bump rules -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "bump"),
    [
        ("feat: x", "MINOR"),
        ("feat(sdk): x (#19)", "MINOR"),
        ("fix: x", "PATCH"),
        ("perf: x", "PATCH"),
        ("security: x", "PATCH"),
        ("refactor(core): x", "PATCH"),
        ("revert: feat: x", "PATCH"),
        ("build(deps): bump x", "PATCH"),
        ("chore(deps): bump x", "PATCH"),
        ("build: new base image", "NONE"),
        ("chore: tidy", "NONE"),
        ("docs: x", "NONE"),
        ("test: x", "NONE"),
        ("ci: x", "NONE"),
        ("style: x", "NONE"),
    ],
)
def test_bump_rules(subject: str, bump: str) -> None:
    assert nv.bump_of(commit(subject)) is nv.Bump[bump]


@pytest.mark.parametrize(
    ("previous", "subjects", "expected"),
    [
        ("1.2.3", ["fix: a", "docs: b"], "1.2.4"),
        ("1.2.3", ["fix: a", "feat: b"], "1.3.0"),
        ("1.2.3", ["feat: a", "fix!: b"], "2.0.0"),
        ("0.9.1", ["feat: a"], "0.10.0"),
        ("1.2.3", ["docs: a", "ci: b", "Merge things"], None),
        ("1.2.3", [], None),
    ],
)
def test_next_release_takes_the_largest_bump(
    previous: str, subjects: list[str], expected: str | None
) -> None:
    result = nv.next_release(ver(previous), [commit(s) for s in subjects])
    assert (result.text if result else None) == expected


def test_the_first_release_is_1_0_0_whatever_the_commits() -> None:
    assert nv.next_release(None, [commit("docs: only docs")]) == nv.Version(1, 0, 0)


# --- tags and candidates ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["contractum-v0.1.0", "v1.2", "v01.2.3", "v1.2.3-rc.0", "v1.2.3-beta.1", "1.2.3", ""]
)
def test_only_vX_Y_Z_and_its_release_candidates_are_release_tags(name: str) -> None:
    assert nv.parse_tag(name) is None


def test_a_release_candidate_sorts_below_its_stable_version() -> None:
    ordered = sorted(
        [ver("1.2.0"), ver("1.2.0-rc.10"), ver("1.2.0-rc.2"), ver("1.1.9")], key=lambda v: v.key
    )
    assert [v.text for v in ordered] == ["1.1.9", "1.2.0-rc.2", "1.2.0-rc.10", "1.2.0"]
    assert nv.latest_stable(ordered) == ver("1.2.0")
    assert nv.latest_stable([ver("1.0.0-rc.1")]) is None


@pytest.mark.parametrize(
    ("text", "python"),
    [("1.2.0", "1.2.0"), ("1.2.0-rc.1", "1.2.0rc1"), ("10.0.3-rc.12", "10.0.3rc12")],
)
def test_the_sdk_takes_the_pep_440_form_of_the_version(text: str, python: str) -> None:
    assert ver(text).python == python
    assert cl.python_version(text) == python


def test_the_notes_refuse_a_version_that_is_not_a_release_version() -> None:
    with pytest.raises(ValueError, match="not a release version"):
        cl.python_version("1.2")


def test_the_candidate_number_is_one_above_the_highest_of_that_version() -> None:
    tags = [ver("1.3.0-rc.1"), ver("1.3.0-rc.3"), ver("1.4.0-rc.7"), ver("1.2.0")]
    assert nv.next_candidate(ver("1.3.0"), tags).text == "1.3.0-rc.4"
    assert nv.next_candidate(ver("2.0.0"), tags).text == "2.0.0-rc.1"


def test_pre_release_plans_the_next_candidate_of_the_head() -> None:
    tags = {ver("1.2.0"): "s" * 40, ver("1.3.0-rc.1"): "r" * 40}
    plan = nv.plan_pre_release(tags, "h" * 40, [commit("feat: a")])
    assert plan.outputs() == {
        "version": "1.3.0-rc.2",
        "python-version": "1.3.0rc2",
        "tag": "v1.3.0-rc.2",
        "sha": "h" * 40,
        "previous": "v1.2.0",
        "prerelease": "true",
    }


def test_pre_release_without_any_tag_is_the_first_candidate_of_1_0_0() -> None:
    plan = nv.plan_pre_release({}, "h" * 40, [commit("chore: start")])
    assert (plan.version.tag, plan.outputs()["previous"]) == ("v1.0.0-rc.1", "")


def test_pre_release_stops_when_nothing_releasable_landed() -> None:
    with pytest.raises(nv.ReleaseStopError, match=r"nothing to release.*since v1\.2\.0"):
        nv.plan_pre_release({ver("1.2.0"): "s" * 40}, "h" * 40, [commit("docs: a")])


def test_pre_release_stops_when_the_head_already_is_that_candidate() -> None:
    tags = {ver("1.2.0"): "s" * 40, ver("1.2.1-rc.1"): "h" * 40}
    with pytest.raises(nv.ReleaseStopError, match=r"already the release candidate v1\.2\.1-rc\.1"):
        nv.plan_pre_release(tags, "h" * 40, [commit("fix: a")])


def test_promote_releases_the_commit_of_the_newest_candidate() -> None:
    tags = {ver("1.2.0"): "s" * 40, ver("1.3.0-rc.1"): "1" * 40, ver("1.3.0-rc.2"): "2" * 40}
    plan = nv.plan_promote(tags)
    assert plan.outputs() == {
        "version": "1.3.0",
        "python-version": "1.3.0",
        "tag": "v1.3.0",
        "sha": "2" * 40,
        "previous": "v1.2.0",
        "prerelease": "false",
    }


def test_promote_stops_without_a_candidate() -> None:
    with pytest.raises(nv.ReleaseStopError, match="no release candidate"):
        nv.plan_promote({ver("1.2.0"): "s" * 40})


def test_promote_stops_when_the_newest_candidate_was_already_promoted() -> None:
    tags = {ver("1.3.0-rc.2"): "2" * 40, ver("1.3.0"): "2" * 40}
    with pytest.raises(nv.ReleaseStopError, match="already promoted"):
        nv.plan_promote(tags)


# --- changelog ------------------------------------------------------------------------------------


def test_the_changes_are_grouped_by_kind_and_sorted_by_scope() -> None:
    commits = [  # newest first, as git log lists them
        commit("docs: left out (#9)", sha="9" * 40),
        commit("Update nats-py (#8)", sha="8" * 40),
        commit("fix(security): token check (#7)", sha="7" * 40),
        commit("perf(contracts): faster list (#6)", sha="6" * 40),
        commit("fix(ui): button", sha="5" * 40),
        commit("fix(api): problem slug (#4)", sha="4" * 40),
        commit("feat: unscoped feature (#3)", sha="3" * 40),
        commit("feat(sdk): pull command (#2)", sha="2" * 40),
        commit("feat(api)!: drop v0 (#1)", sha="1" * 40),
    ]
    text = cl.changes(commits, REPO_URL, "v1.2.0")
    assert text.splitlines()[:3] == ["## Changes", "", "Since v1.2.0."]
    sections = [line for line in text.splitlines() if line.startswith("### ")]
    assert sections == [
        "### Breaking changes",
        "### Features",
        "### Fixes",
        "### Security",
        "### Performance",
        "### Other changes",
    ]
    assert f"- **api:** Drop v0 ([#1]({REPO_URL}/pull/1))" in text
    features = text.split("### Features")[1].split("###")[0]
    assert features.index("**sdk:** Pull command") < features.index("- Unscoped feature")
    fixes = text.split("### Fixes")[1].split("###")[0]
    assert fixes.index("**api:**") < fixes.index("**ui:**")
    assert f"- **ui:** Button ([{'5' * 12}]({REPO_URL}/commit/{'5' * 40}))" in fixes
    assert "- Token check ([#7]" in text.split("### Security")[1].split("###")[0]
    assert "- Update nats-py ([#8]" in text.split("### Other changes")[1]
    assert "left out" not in text


def test_a_release_with_only_left_out_commits_says_so() -> None:
    text = cl.changes([commit("ci: x"), commit("test: y")], REPO_URL, None)
    assert "Everything in the first release." in text
    assert "No user-facing changes." in text


def test_the_notes_name_every_image_the_chart_and_the_verification() -> None:
    text = cl.notes(
        "1.3.0", [commit("feat: a (#1)")], "Acme/contractum", "https://github.com/", None
    )
    for name in ("identity", "contracts", "validation", "audit", "mail", "api-docs", "ui"):
        assert f"`ghcr.io/acme/contractum/{name}:1.3.0`" in text
    assert "oci://ghcr.io/acme/charts/contractum --version 1.3.0" in text
    assert "gh attestation verify oci://ghcr.io/acme/contractum/identity:1.3.0" in text
    assert "https://github.com/Acme/contractum/pull/1" in text
    wheel = "https://github.com/Acme/contractum/releases/download/v1.3.0/contractum-1.3.0-py3-none-any.whl"
    assert f"uv tool install {wheel}" in text
    assert f"pip install {wheel}" in text
    assert "`ghcr.io/acme/contractum-cli:1.3.0`" in text
    assert "gh attestation verify contractum-1.3.0-py3-none-any.whl" in text
    assert "sha256sum -c SHA256SUMS --ignore-missing" in text


def test_the_notes_of_a_candidate_name_its_pep_440_wheel_and_its_cli_image() -> None:
    text = cl.notes("1.3.0-rc.2", [], "acme/contractum", "https://github.example", None)
    assert (
        "https://github.example/acme/contractum/releases/download/v1.3.0-rc.2/"
        "contractum-1.3.0rc2-py3-none-any.whl"
    ) in text
    assert "`contractum-1.3.0rc2.tar.gz`" in text
    assert "`ghcr.io/acme/contractum-cli:1.3.0-rc.2`" in text


# --- end to end against a git repository ----------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    done = subprocess.run(  # noqa: S603 - fixed program, test-made arguments
        ["git", *args],  # noqa: S607 - git from PATH, as the release workflow runs it
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _run(script: str, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    return subprocess.run(  # noqa: S603 - this interpreter and a repository script
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    for subject in ("chore: start", "feat: first feature (#1)"):
        _git(tmp_path, "commit", "-q", "--allow-empty", "-m", subject)
    return tmp_path


def _outputs(done: subprocess.CompletedProcess[str]) -> dict[str, str]:
    assert done.returncode == 0, done.stderr
    return dict(line.split("=", 1) for line in done.stdout.splitlines())


def test_the_scripts_walk_a_release_from_candidate_to_stable(repo: Path, tmp_path: Path) -> None:
    first = _outputs(_run("next_version.py", repo, "--mode", "pre-release"))
    assert (first["tag"], first["previous"]) == ("v1.0.0-rc.1", "")
    _git(repo, "tag", "-a", first["tag"], "-m", first["tag"], first["sha"])
    again = _run("next_version.py", repo, "--mode", "pre-release")
    assert again.returncode == 1
    assert "already the release candidate v1.0.0-rc.1" in again.stderr

    output = tmp_path / "github-output"
    promoted = _outputs(
        _run("next_version.py", repo, "--mode", "promote", "--github-output", str(output))
    )
    assert promoted == {
        "version": "1.0.0",
        "python-version": "1.0.0",
        "tag": "v1.0.0",
        "sha": first["sha"],
        "previous": "",
        "prerelease": "false",
    }
    assert "tag=v1.0.0" in output.read_text(encoding="utf-8").splitlines()
    _git(repo, "tag", "v1.0.0", promoted["sha"])  # lightweight, as gh release create makes it
    assert "already promoted" in _run("next_version.py", repo, "--mode", "promote").stderr

    _git(repo, "commit", "-q", "--allow-empty", "-m", "docs: only docs")
    assert "nothing to release" in _run("next_version.py", repo, "--mode", "pre-release").stderr
    _git(repo, "commit", "-q", "--allow-empty", "-m", "fix(api): a fix (#2)")
    patch = _outputs(_run("next_version.py", repo, "--mode", "pre-release"))
    assert (patch["tag"], patch["previous"]) == ("v1.0.1-rc.1", "v1.0.0")

    notes = tmp_path / "notes.md"
    done = _run(
        "changelog.py",
        repo,
        "--version",
        patch["version"],
        "--to",
        patch["sha"],
        "--since",
        patch["previous"],
        "--repo",
        "acme/contractum",
        "--output",
        str(notes),
    )
    assert done.returncode == 0, done.stderr
    text = notes.read_text(encoding="utf-8")
    assert "**api:** A fix ([#2](https://github.com/acme/contractum/pull/2))" in text
    assert "First feature" not in text  # released in v1.0.0
    assert "ghcr.io/acme/contractum/ui:1.0.1-rc.1" in text
    assert "/releases/download/v1.0.1-rc.1/contractum-1.0.1rc1-py3-none-any.whl" in text
