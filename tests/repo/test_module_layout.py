"""PLAT-01: every module folder describes itself with its own CLAUDE.md and skills.

The repository is one folder per module (identity, contracts, validation, audit, mail, gateway,
shared, ui). Each owns its documentation: a non-empty ``CLAUDE.md`` and at least one
``.claude/skills/<name>/SKILL.md`` with a non-empty ``name`` and ``description``, all regular
files inside that module (nothing symlinked or shared between modules). The repository root
carries the cross-cutting skills (run-stack, add-route, add-migration, keep-docs-current, security-first). Every service that
declares routes also has its route-policy walker test (PLAT-04).

Nested skill auto-discovery is unverified (research A16), so each module's CLAUDE.md must list the
path of every skill the module owns.

``collect_violations`` is pure and takes the repository root, so the edge cases below run against
synthetic trees. Modules are visited in sorted order and violations are returned sorted, so the
failure message is identical across runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MODULES = ("audit", "contracts", "gateway", "identity", "mail", "shared", "ui", "validation")
SERVICES_WITH_WALKER = ("audit", "contracts", "identity", "mail", "validation")
ROOT_SKILLS = ("add-migration", "add-route", "keep-docs-current", "run-stack", "security-first")
WALKER_TEST = "tests/unit/test_route_policies.py"
WALKER_CALL = "assert_all_routes_have_policy"


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _frontmatter(text: str) -> dict[str, object] | None:
    """The YAML frontmatter block of a SKILL.md, or None when absent or malformed."""
    if not text.startswith("---\n"):
        return None
    head, separator, _ = text[4:].partition("\n---")
    if not separator:
        return None
    try:
        loaded = yaml.safe_load(head)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _skill_files(skills_dir: Path) -> list[Path]:
    return sorted(path for path in skills_dir.iterdir() if path.is_dir() or path.is_symlink())


def _check_skills(
    label: str, skills_dir: Path, names: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Violations for one skills directory plus the SKILL.md paths it holds (relative to it)."""
    violations: list[str] = []
    found: list[str] = []
    if skills_dir.is_symlink():
        violations.append(f"{label}: .claude/skills is a symlink (modules must not share skills)")
    if not skills_dir.is_dir():
        return [f"{label}: .claude/skills is missing"], found
    entries = _skill_files(skills_dir)
    if not entries:
        violations.append(f"{label}: .claude/skills holds no skill")
    for entry in entries:
        skill = entry / "SKILL.md"
        where = f"{label}: .claude/skills/{entry.name}/SKILL.md"
        if entry.is_symlink():
            violations.append(f"{label}: .claude/skills/{entry.name} is a symlink")
            continue
        if not skill.exists() and not skill.is_symlink():
            violations.append(f"{where} is missing")
            continue
        if not _is_regular_file(skill):
            violations.append(f"{where} is not a regular file (symlinks are not allowed)")
            continue
        found.append(f"{entry.name}/SKILL.md")
        meta = _frontmatter(skill.read_text(encoding="utf-8"))
        if meta is None:
            violations.append(f"{where} has no YAML frontmatter")
            continue
        name = meta.get("name")
        description = meta.get("description")
        if not isinstance(name, str) or not name.strip():
            violations.append(f"{where} has no non-empty name")
        else:
            previous = names.get(name.strip())
            if previous is not None:
                violations.append(
                    f"{where}: skill name {name.strip()!r} is also used by {previous}"
                )
            else:
                names[name.strip()] = where
        if not isinstance(description, str) or not description.strip():
            violations.append(f"{where} has no non-empty description")
    return violations, found


def collect_violations(
    root: Path,
    *,
    modules: tuple[str, ...] = MODULES,
    services: tuple[str, ...] = SERVICES_WITH_WALKER,
    root_skills: tuple[str, ...] = ROOT_SKILLS,
) -> list[str]:
    """Every layout violation under ``root``, sorted, as human-readable lines."""
    violations: list[str] = []
    names: dict[str, str] = {}

    for module in sorted(modules):
        folder = root / module
        if folder.is_symlink() or not folder.is_dir():
            violations.append(f"{module}: module folder is missing or a symlink")
            continue
        claude = folder / "CLAUDE.md"
        if not claude.exists() and not claude.is_symlink():
            violations.append(f"{module}: CLAUDE.md is missing")
            claude_text = ""
        elif not _is_regular_file(claude):
            violations.append(
                f"{module}: CLAUDE.md is not a regular file (symlinks are not allowed)"
            )
            claude_text = ""
        else:
            claude_text = claude.read_text(encoding="utf-8")
            if not claude_text.strip():
                violations.append(f"{module}: CLAUDE.md is empty")
        skill_violations, skills = _check_skills(module, folder / ".claude" / "skills", names)
        violations.extend(skill_violations)
        for skill_path in skills:
            listed = f".claude/skills/{skill_path}"
            if claude_text.strip() and listed not in claude_text:
                violations.append(f"{module}: CLAUDE.md does not list {listed}")

    for skill_name in sorted(root_skills):
        root_skill = root / ".claude" / "skills" / skill_name / "SKILL.md"
        if not _is_regular_file(root_skill):
            violations.append(f"root: .claude/skills/{skill_name}/SKILL.md is missing")
    root_violations, _ = _check_skills("root", root / ".claude" / "skills", names)
    violations.extend(root_violations)

    for service in sorted(services):
        walker = root / service / WALKER_TEST
        if not _is_regular_file(walker):
            violations.append(f"{service}: {WALKER_TEST} is missing")
        elif WALKER_CALL not in walker.read_text(encoding="utf-8"):
            violations.append(f"{service}: {WALKER_TEST} does not call {WALKER_CALL}")

    return sorted(set(violations))


def test_every_module_has_its_own_claude_md_and_skills() -> None:
    violations = collect_violations(ROOT)
    assert not violations, "module layout violations:\n" + "\n".join(violations)


def test_the_repo_root_instructions_are_a_regular_file_inside_the_repo() -> None:
    assert _is_regular_file(ROOT / ".claude" / "CLAUDE.md")


# --- edge cases, against synthetic trees ------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill(path: Path, name: str, description: str = "does a thing") -> Path:
    return _write(
        path / "SKILL.md", f"---\nname: {name}\ndescription: {description}\n---\n\nSteps.\n"
    )


def _healthy_tree(root: Path, modules: tuple[str, ...] = ("alpha", "beta")) -> None:
    for module in modules:
        skill = f"{module}-skill"
        _write(root / module / "CLAUDE.md", f"# {module}\n\n- .claude/skills/{skill}/SKILL.md\n")
        _skill(root / module / ".claude" / "skills" / skill, skill)
    for name in ROOT_SKILLS:
        _skill(root / ".claude" / "skills" / name, name)


def _collect(root: Path, modules: tuple[str, ...] = ("alpha", "beta")) -> list[str]:
    return collect_violations(root, modules=modules, services=())


def test_a_complete_synthetic_tree_has_no_violations(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    assert _collect(tmp_path) == []


def test_adjacency_symlinked_claude_md_is_a_violation(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    (tmp_path / "beta" / "CLAUDE.md").unlink()
    (tmp_path / "beta" / "CLAUDE.md").symlink_to(tmp_path / "alpha" / "CLAUDE.md")
    assert any("beta: CLAUDE.md is not a regular file" in v for v in _collect(tmp_path))


def test_adjacency_a_skill_file_shared_between_modules_is_a_violation(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    shared = tmp_path / "beta" / ".claude" / "skills" / "beta-skill" / "SKILL.md"
    shared.unlink()
    shared.symlink_to(tmp_path / "alpha" / ".claude" / "skills" / "alpha-skill" / "SKILL.md")
    assert any(
        "beta: .claude/skills/beta-skill/SKILL.md is not a regular file" in v
        for v in _collect(tmp_path)
    )


def test_adjacency_a_symlinked_skills_directory_is_a_violation(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    skills = tmp_path / "beta" / ".claude" / "skills"
    for child in list(skills.iterdir()):
        for leaf in child.iterdir():
            leaf.unlink()
        child.rmdir()
    skills.rmdir()
    skills.symlink_to(tmp_path / "alpha" / ".claude" / "skills", target_is_directory=True)
    assert any("beta: .claude/skills is a symlink" in v for v in _collect(tmp_path))


def test_adjacency_skill_names_must_be_unique_across_modules(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    _skill(tmp_path / "beta" / ".claude" / "skills" / "beta-skill", "alpha-skill")
    violations = _collect(tmp_path)
    assert any("skill name 'alpha-skill' is also used by" in v for v in violations)


@pytest.mark.parametrize("content", ["", "   \n\n"])
def test_empty_claude_md_is_a_violation(tmp_path: Path, content: str) -> None:
    _healthy_tree(tmp_path)
    (tmp_path / "alpha" / "CLAUDE.md").write_text(content, encoding="utf-8")
    assert "alpha: CLAUDE.md is empty" in _collect(tmp_path)


def test_empty_missing_claude_md_is_a_violation(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    (tmp_path / "alpha" / "CLAUDE.md").unlink()
    assert "alpha: CLAUDE.md is missing" in _collect(tmp_path)


def test_empty_missing_skills_directory_is_a_violation(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    (tmp_path / "alpha" / ".claude" / "skills" / "alpha-skill" / "SKILL.md").unlink()
    (tmp_path / "alpha" / ".claude" / "skills" / "alpha-skill").rmdir()
    (tmp_path / "alpha" / ".claude" / "skills").rmdir()
    assert "alpha: .claude/skills is missing" in _collect(tmp_path)


def test_empty_skills_directory_without_a_skill_is_a_violation(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    (tmp_path / "alpha" / ".claude" / "skills" / "alpha-skill" / "SKILL.md").unlink()
    (tmp_path / "alpha" / ".claude" / "skills" / "alpha-skill").rmdir()
    assert "alpha: .claude/skills holds no skill" in _collect(tmp_path)


def test_empty_skill_directory_without_skill_md_is_a_violation(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    (tmp_path / "alpha" / ".claude" / "skills" / "alpha-skill" / "SKILL.md").unlink()
    assert "alpha: .claude/skills/alpha-skill/SKILL.md is missing" in _collect(tmp_path)


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ("---\nname: \ndescription: x\n---\n", "has no non-empty name"),
        ("---\ndescription: x\n---\n", "has no non-empty name"),
        ("---\nname: x\ndescription: ''\n---\n", "has no non-empty description"),
        ("---\nname: x\n---\n", "has no non-empty description"),
        ("no frontmatter at all\n", "has no YAML frontmatter"),
    ],
)
def test_empty_skill_frontmatter_fields_are_violations(
    tmp_path: Path, frontmatter: str, expected: str
) -> None:
    _healthy_tree(tmp_path)
    (tmp_path / "alpha" / ".claude" / "skills" / "alpha-skill" / "SKILL.md").write_text(
        frontmatter, encoding="utf-8"
    )
    assert any(expected in v for v in _collect(tmp_path))


def test_a_module_claude_md_must_list_its_skills(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    (tmp_path / "alpha" / "CLAUDE.md").write_text(
        "# alpha\n\nNo skills listed.\n", encoding="utf-8"
    )
    assert "alpha: CLAUDE.md does not list .claude/skills/alpha-skill/SKILL.md" in _collect(
        tmp_path
    )


def test_missing_root_skills_are_violations(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    (tmp_path / ".claude" / "skills" / "run-stack" / "SKILL.md").unlink()
    assert "root: .claude/skills/run-stack/SKILL.md is missing" in _collect(tmp_path)


def test_a_service_without_its_route_policy_walker_test_is_a_violation(tmp_path: Path) -> None:
    _healthy_tree(tmp_path)
    assert "alpha: tests/unit/test_route_policies.py is missing" in collect_violations(
        tmp_path, modules=("alpha", "beta"), services=("alpha", "beta")
    )
    _write(tmp_path / "alpha" / WALKER_TEST, "def test_x() -> None: ...\n")
    _write(tmp_path / "beta" / WALKER_TEST, f"{WALKER_CALL}(app)\n")
    violations = collect_violations(tmp_path, modules=("alpha", "beta"), services=("alpha", "beta"))
    assert f"alpha: {WALKER_TEST} does not call {WALKER_CALL}" in violations
    assert not any(v.startswith("beta:") for v in violations)


def test_ordering_all_violations_are_reported_together_in_a_stable_sorted_order(
    tmp_path: Path,
) -> None:
    _healthy_tree(tmp_path, modules=("alpha", "beta", "gamma"))
    (tmp_path / "gamma" / "CLAUDE.md").write_text("", encoding="utf-8")
    (tmp_path / "alpha" / "CLAUDE.md").unlink()
    (tmp_path / "beta" / ".claude" / "skills" / "beta-skill" / "SKILL.md").unlink()
    modules = ("gamma", "alpha", "beta")  # deliberately unsorted input
    first = collect_violations(tmp_path, modules=modules, services=())
    second = collect_violations(tmp_path, modules=modules, services=())
    assert first == second == sorted(first)
    joined = "\n".join(first)
    assert "alpha: CLAUDE.md is missing" in joined
    assert "beta: .claude/skills/beta-skill/SKILL.md is missing" in joined
    assert "gamma: CLAUDE.md is empty" in joined


def test_ordering_the_real_module_list_is_sorted() -> None:
    assert list(MODULES) == sorted(MODULES)
    assert list(SERVICES_WITH_WALKER) == sorted(SERVICES_WITH_WALKER)
    assert list(ROOT_SKILLS) == sorted(ROOT_SKILLS)


# --- the mail module (D-105) is held to the same gates -----------------------------------------


def test_the_mail_module_is_covered_by_the_layout_and_walker_gates() -> None:
    assert "mail" in MODULES
    assert "mail" in SERVICES_WITH_WALKER


def test_a_mail_module_without_claude_md_is_reported(tmp_path: Path) -> None:
    _healthy_tree(tmp_path, modules=("alpha", "mail"))
    assert collect_violations(tmp_path, modules=("alpha", "mail"), services=()) == []
    (tmp_path / "mail" / "CLAUDE.md").unlink()
    violations = collect_violations(tmp_path, modules=("alpha", "mail"), services=())
    assert "mail: CLAUDE.md is missing" in violations


def test_a_mail_module_without_its_walker_test_is_reported(tmp_path: Path) -> None:
    _healthy_tree(tmp_path, modules=("alpha", "mail"))
    modules = ("alpha", "mail")
    assert f"mail: {WALKER_TEST} is missing" in collect_violations(
        tmp_path, modules=modules, services=("mail",)
    )
    _write(tmp_path / "mail" / WALKER_TEST, "def test_x() -> None: ...\n")
    assert f"mail: {WALKER_TEST} does not call {WALKER_CALL}" in collect_violations(
        tmp_path, modules=modules, services=("mail",)
    )
    _write(tmp_path / "mail" / WALKER_TEST, f"{WALKER_CALL}(app)\n")
    assert collect_violations(tmp_path, modules=modules, services=("mail",)) == []
