"""The docs-only CI skip: scripts/ci_changes.py and its wiring in .github/workflows/ci.yml.

A change that touches only documentation files skips the code jobs, runs the repo tests instead
and still reports through the required ``ci-ok`` job; any other change runs everything. A failed,
cancelled or unexpectedly skipped job always fails ``ci-ok``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ci_changes.py"
CI = REPO / ".github" / "workflows" / "ci.yml"
SHA_PINNED = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_changes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_changes"] = module
    spec.loader.exec_module(module)
    return module


cc = _load()


def _workflow() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    return workflow


def _jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = _workflow()["jobs"]
    return jobs


def test_the_docs_filter_list_is_exactly_the_documentation_files() -> None:
    assert cc.DOCS_PATTERNS == (
        "docs/**",
        "*.md",
        "mkdocs.yml",
        ".markdownlint-cli2.jsonc",
        "_typos.toml",
        "lychee.toml",
    )


@pytest.mark.parametrize(
    "path",
    [
        "docs/index.md",
        "docs/dev/ci-and-github.md",
        "docs/dev/architecture/images/01-component-map.svg",
        "docs/user/images/sign-in.png",
        "docs/dev/architecture/01-component-map.drawio",
        "README.md",
        "CONTRIBUTING.md",
        "ui/CLAUDE.md",
        ".claude/skills/security-first/SKILL.md",
        "mkdocs.yml",
        ".markdownlint-cli2.jsonc",
        "_typos.toml",
        "lychee.toml",
    ],
)
def test_documentation_paths_are_docs(path: str) -> None:
    assert cc.is_docs_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "identity/src/dc_identity/api/auth.py",
        "ui/src/main.tsx",
        "ui/package.json",
        ".github/workflows/ci.yml",
        ".github/workflows/docs.yml",
        "scripts/ci_changes.py",
        "pyproject.toml",
        "uv.lock",
        "deploy/helm/contractum/values.yaml",
        "infra/docker/ui.Dockerfile",
        "README.md.bak",
        "notes.markdown",
        "sub/mkdocs.yml",
        "docs",  # a file named docs at the root is not the docs folder
        "docsite/index.md.txt",
    ],
)
def test_every_other_path_runs_the_full_ci(path: str) -> None:
    assert not cc.is_docs_path(path)


def test_docs_only_needs_a_non_empty_all_docs_change() -> None:
    assert cc.docs_only(["docs/index.md", "README.md"])
    assert not cc.docs_only(["docs/index.md", "ui/src/main.tsx"])
    assert not cc.docs_only([])
    assert not cc.docs_only([""])


def _classify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paths: list[str] | None) -> str:
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    paths_file = tmp_path / "changed.txt"
    if paths is not None:
        paths_file.write_text("\n".join(paths) + "\n", encoding="utf-8")
    assert cc.main(["classify", "--paths-file", str(paths_file)]) == 0
    return output.read_text(encoding="utf-8")


def test_classify_writes_true_for_a_docs_only_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _classify(tmp_path, monkeypatch, ["docs/index.md", "lychee.toml"]) == (
        "docs_only=true\n"
    )


def test_classify_writes_false_for_a_code_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _classify(tmp_path, monkeypatch, ["docs/index.md", "uv.lock"]) == "docs_only=false\n"


def test_classify_fails_safe_when_the_paths_are_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _classify(tmp_path, monkeypatch, None) == "docs_only=false\n"


def test_classify_fails_safe_on_an_empty_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _classify(tmp_path, monkeypatch, []) == "docs_only=false\n"


def _needs(**results: str) -> dict[str, dict[str, Any]]:
    base = {job: "success" for job in cc.ALWAYS_JOBS | cc.CODE_JOBS}
    base.update({job: "skipped" for job in cc.DOCS_ONLY_JOBS})
    base.update({key.replace("_", "-"): value for key, value in results.items()})
    return {job: {"result": result, "outputs": {}} for job, result in base.items()}


def _docs_only_needs(**results: str) -> dict[str, dict[str, Any]]:
    needs = _needs(**{job.replace("-", "_"): "skipped" for job in cc.CODE_JOBS})
    for job in cc.DOCS_ONLY_JOBS:
        needs[job]["result"] = "success"
    for key, value in results.items():
        needs[key.replace("_", "-")]["result"] = value
    return needs


def test_a_full_run_passes_when_every_code_job_succeeds() -> None:
    assert cc.verdict(_needs(), docs_only_change=False) == []


def test_a_docs_only_run_passes_with_the_code_jobs_skipped() -> None:
    assert cc.verdict(_docs_only_needs(), docs_only_change=True) == []


@pytest.mark.parametrize("job", sorted(cc.CODE_JOBS))
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_a_full_run_fails_on_any_code_job_that_did_not_succeed(job: str, result: str) -> None:
    needs = _needs()
    needs[job]["result"] = result
    assert cc.verdict(needs, docs_only_change=False) == [f"{job}: {result}"]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_a_docs_only_run_fails_when_the_repo_tests_did_not_succeed(result: str) -> None:
    needs = _docs_only_needs(docs_repo_tests=result)
    assert cc.verdict(needs, docs_only_change=True) == [f"docs-repo-tests: {result}"]


@pytest.mark.parametrize("job", sorted(cc.CODE_JOBS))
@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_a_docs_only_run_still_fails_when_a_code_job_ran_and_failed(job: str, result: str) -> None:
    needs = _docs_only_needs()
    needs[job]["result"] = result
    assert cc.verdict(needs, docs_only_change=True) == [f"{job}: {result}"]


@pytest.mark.parametrize("docs_only_change", [True, False])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_the_changes_job_must_always_succeed(docs_only_change: bool, result: str) -> None:
    needs = _docs_only_needs() if docs_only_change else _needs()
    needs["changes"]["result"] = result
    assert cc.verdict(needs, docs_only_change=docs_only_change) == [f"changes: {result}"]


def test_a_failed_classification_is_not_a_docs_only_run() -> None:
    # `changes` failed, so its output is empty and every code job was skipped by its `if:`.
    needs = _needs(changes="failure", **{job.replace("-", "_"): "skipped" for job in cc.CODE_JOBS})
    problems = cc.verdict(needs, docs_only_change=False)
    assert "changes: failure" in problems
    assert len(problems) == 1 + len(cc.CODE_JOBS)


def test_the_verdict_refuses_a_missing_or_unknown_job() -> None:
    needs = _needs()
    del needs["ui"]
    needs["new-job"] = {"result": "success"}
    problems = cc.verdict(needs, docs_only_change=False)
    assert "ci-ok does not wait for: ui" in problems
    assert "ci-ok does not know the rule for: new-job" in problems


def test_verdict_command_exit_codes() -> None:
    assert cc.main(["verdict", "--needs", json.dumps(_needs()), "--docs-only", "false"]) == 0
    failing = _needs(ui="failure")
    assert cc.main(["verdict", "--needs", json.dumps(failing), "--docs-only", "false"]) == 1
    assert (
        cc.main(["verdict", "--needs", json.dumps(_docs_only_needs()), "--docs-only", "true"]) == 0
    )


# --- the workflow wiring -------------------------------------------------------------------------


def test_the_workflow_triggers_are_unchanged() -> None:
    triggers = _workflow()[True]  # PyYAML reads the `on:` key as True
    assert set(triggers) == {"pull_request", "push"}
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["pull_request"] is None  # no paths filter: ci-ok must always report


def test_the_job_sets_in_the_workflow_match_the_script() -> None:
    jobs = _jobs()
    assert set(jobs) == cc.ALWAYS_JOBS | cc.CODE_JOBS | cc.DOCS_ONLY_JOBS | {"ci-ok"}


def test_the_changes_job_diffs_the_full_history_without_a_third_party_action() -> None:
    job = _jobs()["changes"]
    assert "needs" not in job
    assert job["outputs"] == {"docs_only": "${{ steps.classify.outputs.docs_only }}"}
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    assert len(uses) == 1 and uses[0].startswith("actions/checkout@")
    checkout = job["steps"][0]
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 0}
    classify = next(step for step in job["steps"] if step.get("id") == "classify")
    assert "git diff --name-only --no-renames" in classify["run"]
    assert "scripts/ci_changes.py classify" in classify["run"]
    # The SHAs reach the shell through env, never interpolated into the script.
    assert "${{" not in classify["run"]
    assert "pull_request.base.sha" in classify["env"]["BASE_SHA"]
    assert "github.event.before" in classify["env"]["BASE_SHA"]


@pytest.mark.parametrize("job", sorted(cc.CODE_JOBS))
def test_every_code_job_is_skipped_only_on_a_docs_only_change(job: str) -> None:
    spec = _jobs()[job]
    assert spec["needs"] == "changes"
    assert spec["if"] == "needs.changes.outputs.docs_only != 'true'"


def test_the_docs_only_repo_tests_run_the_repo_suite() -> None:
    spec = _jobs()["docs-repo-tests"]
    assert spec["needs"] == "changes"
    assert spec["if"] == "needs.changes.outputs.docs_only == 'true'"
    runs = " ".join(step.get("run", "") for step in spec["steps"])
    assert 'uv run pytest tests/repo -m "unit and not performance"' in runs


def test_ci_ok_waits_for_every_job_and_runs_the_verdict() -> None:
    spec = _jobs()["ci-ok"]
    assert spec["name"] == "ci-ok"
    assert spec["if"] == "always()"
    assert set(spec["needs"]) == cc.ALWAYS_JOBS | cc.CODE_JOBS | cc.DOCS_ONLY_JOBS
    summary = spec["steps"][-1]
    assert summary["env"] == {
        "NEEDS": "${{ toJSON(needs) }}",
        "DOCS_ONLY": "${{ needs.changes.outputs.docs_only }}",
    }
    assert summary["run"] == (
        'python3 scripts/ci_changes.py verdict --needs "$NEEDS" --docs-only "$DOCS_ONLY"'
    )


def test_every_action_in_ci_is_pinned_by_sha() -> None:
    for name, job in _jobs().items():
        for step in job.get("steps", []):
            if "uses" in step:
                assert SHA_PINNED.match(step["uses"].split(" ")[0]), (name, step["uses"])


def test_python_unit_reads_the_whole_history_for_the_personal_data_guard() -> None:
    checkout = _jobs()["python-unit"]["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0
