"""The digest manifest and the digest operations of the Release workflow.

``scripts/release/release_images.py`` is loaded by path and called through ``main``;
``scripts/release/tested_images.sh`` runs with logging stand-ins for ``docker``, ``gh``, ``curl``
and ``git`` on ``PATH`` (the pattern of ``test_ci_parts.py``), so no Docker daemon, registry or
GitHub is needed. ``scripts/release/rehearse.sh`` is the check against a real image and registry.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "release"
IMAGES = ("identity", "contracts", "validation", "audit", "mail", "api-docs", "ui")
REGISTRY = "ghcr.io/rkisliak"
VERSION = "1.1.0-rc.1"
COMMIT = "c" * 40
GIT_SHA = "c" * 12
LETTERS = "abcdef0"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_images", SCRIPTS / "release_images.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ri = _load()


def digest(name: str) -> str:
    return "sha256:" + LETTERS[IMAGES.index(name)] * 64


def manifest_data(*, tested: bool = False, registry: str = REGISTRY) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": 1,
        "version": VERSION,
        "tag": f"v{VERSION}",
        "commit": COMMIT,
        "git_sha": GIT_SHA,
        "registry": registry,
        "images": {
            name: {"repository": f"{registry}/contractum/{name}", "digest": digest(name)}
            for name in IMAGES
        },
    }
    if tested:
        data["tested"] = {"run_id": "123", "e2e": "success", "dast": "success"}
    return data


def write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def outside_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error prefix depends on GITHUB_ACTIONS, so each test sets it instead of inheriting it."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = ri.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def digests_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "digests"
    directory.mkdir()
    for name in IMAGES:
        (directory / name).write_text(digest(name) + "\n", encoding="utf-8")
    return directory


def assemble_args(tmp_path: Path, directory: Path) -> list[str]:
    return [
        "assemble",
        "--digests",
        str(directory),
        "--version",
        VERSION,
        "--commit",
        COMMIT,
        "--git-sha",
        GIT_SHA,
        "--registry",
        REGISTRY,
        "--output",
        str(tmp_path / "release-images.json"),
    ]


# --- assemble ------------------------------------------------------------------------------------


def test_assemble_writes_the_strict_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = run(assemble_args(tmp_path, digests_dir(tmp_path)), capsys)
    assert code == 0
    written = json.loads((tmp_path / "release-images.json").read_text(encoding="utf-8"))
    assert written == manifest_data()
    assert list(written["images"]) == list(IMAGES)


def test_assemble_refuses_a_missing_image(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = digests_dir(tmp_path)
    (directory / "ui").unlink()
    code, _, err = run(assemble_args(tmp_path, directory), capsys)
    assert code == 1
    assert "exactly the digest files" in err
    assert not (tmp_path / "release-images.json").exists()


def test_assemble_refuses_an_extra_image(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = digests_dir(tmp_path)
    (directory / "extra").write_text(digest("ui"), encoding="utf-8")
    code, _, err = run(assemble_args(tmp_path, directory), capsys)
    assert code == 1
    assert "extra" in err


def test_assemble_refuses_a_malformed_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = digests_dir(tmp_path)
    (directory / "audit").write_text("sha256:" + "A" * 64, encoding="utf-8")
    code, _, err = run(assemble_args(tmp_path, directory), capsys)
    assert code == 1
    assert "digest of audit" in err


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--commit", "c" * 39),
        ("--commit", "C" * 40),
        ("--git-sha", "c" * 11),
        ("--version", "1.1"),
        ("--version", "v1.1.0"),
        ("--version", "1.1.0-rc.0"),
        ("--registry", "GHCR.io/Rkisliak"),
        ("--registry", "ghcr.io/rkisliak/"),
    ],
)
def test_assemble_refuses_a_malformed_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], option: str, value: str
) -> None:
    argv = assemble_args(tmp_path, digests_dir(tmp_path))
    argv[argv.index(option) + 1] = value
    code, _, err = run(argv, capsys)
    assert code == 1
    assert err.startswith("error: ")


def test_errors_carry_the_actions_prefix_under_github_actions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    directory = digests_dir(tmp_path)
    (directory / "ui").unlink()
    code, _, err = run(assemble_args(tmp_path, directory), capsys)
    assert code == 1
    assert err.startswith("::error::")
    assert err.count("\n") == 1


# --- the strict read -----------------------------------------------------------------------------


def _drop_image(data: dict[str, Any]) -> None:
    del data["images"]["mail"]


def _extra_image(data: dict[str, Any]) -> None:
    data["images"]["extra"] = data["images"]["ui"]


def _bad_digest(data: dict[str, Any]) -> None:
    data["images"]["ui"]["digest"] = "sha256:" + "g" * 64


def _short_digest(data: dict[str, Any]) -> None:
    data["images"]["ui"]["digest"] = "sha256:" + "a" * 63


def _no_algorithm(data: dict[str, Any]) -> None:
    data["images"]["ui"]["digest"] = "a" * 64


def _upper_commit(data: dict[str, Any]) -> None:
    data["commit"] = "C" * 40


def _bad_git_sha(data: dict[str, Any]) -> None:
    data["git_sha"] = "c" * 40


def _bad_version(data: dict[str, Any]) -> None:
    data["version"] = "1.1.0-beta.1"
    data["tag"] = "v1.1.0-beta.1"


def _wrong_tag(data: dict[str, Any]) -> None:
    data["tag"] = "v1.1.0"


def _foreign_repository(data: dict[str, Any]) -> None:
    data["images"]["ui"]["repository"] = "ghcr.io/other/contractum/ui"


def _unknown_top_key(data: dict[str, Any]) -> None:
    data["note"] = "x"


def _unknown_image_key(data: dict[str, Any]) -> None:
    data["images"]["ui"]["tag"] = "x"


def _wrong_schema(data: dict[str, Any]) -> None:
    data["schema"] = 2


def _bool_schema(data: dict[str, Any]) -> None:
    data["schema"] = True


def _failed_suite(data: dict[str, Any]) -> None:
    data["tested"]["dast"] = "failure"


def _unknown_tested_key(data: dict[str, Any]) -> None:
    data["tested"]["note"] = "x"


def _bad_run_id(data: dict[str, Any]) -> None:
    data["tested"]["run_id"] = "12a"


MUTATIONS = [
    _drop_image,
    _extra_image,
    _bad_digest,
    _short_digest,
    _no_algorithm,
    _upper_commit,
    _bad_git_sha,
    _bad_version,
    _wrong_tag,
    _foreign_repository,
    _unknown_top_key,
    _unknown_image_key,
    _wrong_schema,
    _bool_schema,
    _failed_suite,
    _unknown_tested_key,
    _bad_run_id,
]


@pytest.mark.parametrize("mutate", MUTATIONS, ids=lambda f: f.__name__.strip("_"))
def test_every_read_refuses_a_manifest_that_breaks_a_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mutate: Any
) -> None:
    data = manifest_data(tested=True)
    mutate(data)
    path = write_json(tmp_path / "m.json", data)
    for argv in (["entries", "--manifest", str(path)], ["field", "--manifest", str(path), "tag"]):
        code, out, err = run(argv, capsys)
        assert code == 1, argv
        assert out == ""
        assert err.startswith("error: ")


def test_a_manifest_that_is_not_json_or_not_an_object_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert run(["entries", "--manifest", str(broken)], capsys)[0] == 1
    assert run(["entries", "--manifest", str(write_json(broken, []))], capsys)[0] == 1
    assert run(["entries", "--manifest", str(tmp_path / "missing.json")], capsys)[0] == 1


def test_entries_and_field_read_the_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_json(tmp_path / "m.json", manifest_data(tested=True))
    code, out, _ = run(["entries", "--manifest", str(path)], capsys)
    assert code == 0
    assert out.splitlines() == [
        f"{name}\t{REGISTRY}/contractum/{name}\t{digest(name)}" for name in IMAGES
    ]
    for key, expected in (
        ("version", VERSION),
        ("tag", f"v{VERSION}"),
        ("commit", COMMIT),
        ("git_sha", GIT_SHA),
        ("registry", REGISTRY),
    ):
        assert run(["field", "--manifest", str(path), key], capsys)[1] == expected + "\n"


# --- record and check ----------------------------------------------------------------------------


def record_args(tmp_path: Path, e2e: str = "success", dast: str = "success") -> list[str]:
    return [
        "record",
        "--manifest",
        str(write_json(tmp_path / "release-images.json", manifest_data())),
        "--run-id",
        "123",
        "--e2e",
        e2e,
        "--dast",
        dast,
        "--output",
        str(tmp_path / "tested-images.json"),
    ]


def test_record_adds_the_tested_block(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(record_args(tmp_path), capsys)[0] == 0
    written = json.loads((tmp_path / "tested-images.json").read_text(encoding="utf-8"))
    assert written == manifest_data(tested=True)


@pytest.mark.parametrize(
    ("e2e", "dast"),
    [
        ("failure", "success"),
        ("success", "failure"),
        ("cancelled", "success"),
        ("skipped", "skipped"),
    ],
)
def test_record_refuses_unless_both_suites_succeeded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], e2e: str, dast: str
) -> None:
    code, _, err = run(record_args(tmp_path, e2e, dast), capsys)
    assert code == 1
    assert "not success" in err
    assert not (tmp_path / "tested-images.json").exists()


def test_record_refuses_a_manifest_that_is_already_tested(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = record_args(tmp_path)
    write_json(Path(argv[2]), manifest_data(tested=True))
    assert run(argv, capsys)[0] == 1


def check_args(path: Path, *extra: str, **overrides: str) -> list[str]:
    values = {"version": VERSION, "commit": COMMIT, "registry": REGISTRY} | overrides
    argv = ["check", "--manifest", str(path)]
    for key, value in values.items():
        argv += [f"--{key}", value]
    return argv + list(extra)


def test_check_accepts_the_expected_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_json(tmp_path / "m.json", manifest_data(tested=True))
    assert run(check_args(path, "--tested"), capsys)[0] == 0
    assert run(check_args(write_json(tmp_path / "u.json", manifest_data())), capsys)[0] == 0


@pytest.mark.parametrize(
    "override",
    [{"version": "1.1.0"}, {"commit": "d" * 40}, {"registry": "ghcr.io/other"}],
    ids=["version", "commit", "registry"],
)
def test_check_refuses_another_version_commit_or_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], override: dict[str, str]
) -> None:
    path = write_json(tmp_path / "m.json", manifest_data(tested=True))
    code, _, err = run(check_args(path, "--tested", **override), capsys)
    assert code == 1
    assert "expected" in err


def test_check_tested_refuses_a_manifest_without_the_tested_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_json(tmp_path / "m.json", manifest_data())
    code, _, err = run(check_args(path, "--tested"), capsys)
    assert code == 1
    assert "no tested block" in err


# --- pin-chart and check-rendered ----------------------------------------------------------------

VALUES = """global:
  imageTag: ""
  imageDigests: {}
  imagePullPolicy: IfNotPresent
"""


def test_pin_chart_writes_the_seven_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = write_json(tmp_path / "m.json", manifest_data(tested=True))
    values = tmp_path / "values.yaml"
    values.write_text(VALUES, encoding="utf-8")
    code, _, _ = run(["pin-chart", "--manifest", str(manifest), "--values", str(values)], capsys)
    assert code == 0
    pinned = yaml.safe_load(values.read_text(encoding="utf-8"))["global"]
    assert pinned["imageDigests"] == {name: digest(name) for name in IMAGES}
    assert pinned["imagePullPolicy"] == "IfNotPresent"


@pytest.mark.parametrize("text", ["global:\n  imageTag: ''\n", VALUES + VALUES.split("\n", 1)[1]])
def test_pin_chart_refuses_an_absent_or_duplicated_anchor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    manifest = write_json(tmp_path / "m.json", manifest_data(tested=True))
    values = tmp_path / "values.yaml"
    values.write_text(text, encoding="utf-8")
    code, _, err = run(["pin-chart", "--manifest", str(manifest), "--values", str(values)], capsys)
    assert code == 1
    assert "exactly once" in err
    assert values.read_text(encoding="utf-8") == text


def rendered_text(
    version: str = "1.1.0", skip: str = "", tag_only: str = "", wrong: str = ""
) -> str:
    lines = ["kind: Deployment", "spec:", "  containers:"]
    for name in IMAGES:
        if name == skip:
            continue
        reference = f"{REGISTRY}/contractum/{name}:{version}@{digest(name)}"
        if name == tag_only:
            reference = f"{REGISTRY}/contractum/{name}:{version}"
        if name == wrong:
            reference = f"{REGISTRY}/contractum/{name}:{version}@sha256:{'1' * 64}"
        lines.append(f"    - name: {name}")
        lines.append(f"      image: {reference}")
    lines += ["    - name: traefik", "      image: traefik:v3.7.19", "    - name: db"]
    lines += ['      image: "postgres:18"']
    return "\n".join(lines) + "\n"


def check_rendered(
    tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    manifest = write_json(tmp_path / "m.json", manifest_data(tested=True))
    rendered = tmp_path / "rendered.yaml"
    rendered.write_text(text, encoding="utf-8")
    code, _, err = run(
        [
            "check-rendered",
            "--manifest",
            str(manifest),
            "--version",
            "1.1.0",
            "--rendered",
            str(rendered),
        ],
        capsys,
    )
    return code, err


def test_check_rendered_accepts_every_platform_image_at_its_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check_rendered(tmp_path, rendered_text(), capsys)[0] == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tag_only": "audit"}, "audit image"),
        ({"wrong": "ui"}, "ui image"),
        ({"skip": "mail"}, "no image for: mail"),
        ({"version": "1.0.0"}, "expected"),
    ],
    ids=["tag-only", "wrong-digest", "missing", "wrong-version"],
)
def test_check_rendered_refuses_an_image_that_is_not_pinned_exactly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kwargs: dict[str, str], message: str
) -> None:
    code, err = check_rendered(tmp_path, rendered_text(**kwargs), capsys)
    assert code == 1
    assert message in err


def test_check_rendered_refuses_a_platform_image_from_another_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = rendered_text() + "      image: registry.example/contractum/ui:1.1.0\n"
    assert check_rendered(tmp_path, text, capsys)[0] == 1


# --- tested_images.sh ----------------------------------------------------------------------------

STUB_LOG = 'printf "%s %s\\n" "$(basename "$0")" "$*" >>"$STUB_LOG"\n'

DOCKER = (
    """#!/usr/bin/env bash
"""
    + STUB_LOG
    + """
name_of() { local ref="${1%%@*}"; ref="${ref%%:*}"; echo "${ref##*/}"; }
case "$1" in
  pull | tag) exit 0 ;;
  image)
    ref="${@: -1}"
    case "$*" in
      *RepoDigests*) [ -n "${STUB_NO_REPODIGEST:-}" ] || echo "$ref" ;;
      *) echo "id-$(name_of "$ref")" ;;
    esac
    ;;
  ps) cat "$STUB_DIR/containers" ;;
  inspect)
    case "$*" in
      *Config.Env*) cat "$STUB_DIR/env-${@: -1}" ;;
      *) cat "$STUB_DIR/info-${@: -1}" ;;
    esac
    ;;
  exec) cat "$STUB_DIR/ui-config.json" ;;
  buildx)
    if [ "$3" = "inspect" ]; then
      name="$(name_of "$4")"
      if [ -f "$STUB_DIR/tag-$name" ]; then
        printf '"%s"\\n' "$(cat "$STUB_DIR/tag-$name")"
      else
        printf '"%s"\\n' "$(cat "$STUB_DIR/tag-digest")"
      fi
    fi
    ;;
esac
"""
)

GH = (
    """#!/usr/bin/env bash
"""
    + STUB_LOG
    + """
if [ -n "${STUB_GH_FAIL:-}" ]; then
  case "$*" in *"$STUB_GH_FAIL"*) exit 1 ;; esac
fi
"""
)

CURL = (
    """#!/usr/bin/env bash
"""
    + STUB_LOG
    + """
module="${@: -1}"
module="${module%/version}"
cat "$STUB_DIR/route-${module##*/}.json"
"""
)

GIT = (
    """#!/usr/bin/env bash
"""
    + STUB_LOG
    + """
echo "$STUB_HEAD"
"""
)

STACK = {
    "identity": ("c1", "c2"),
    "contracts": ("c3",),
    "validation": ("c4",),
    "audit": ("c5",),
    "mail": ("c6",),
    "api-docs": ("c7",),
    "ui": ("c8",),
}


@pytest.fixture
def bench(tmp_path: Path) -> Path:
    """Stand-ins on PATH, a manifest and a stack that runs exactly the release images."""
    stub = tmp_path / "stub"
    (stub / "bin").mkdir(parents=True)
    for name, body in (("docker", DOCKER), ("gh", GH), ("curl", CURL), ("git", GIT)):
        path = stub / "bin" / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    write_json(tmp_path / "tested-images.json", manifest_data(tested=True))
    write_stack(stub)
    return tmp_path


def write_stack(stub: Path, **changes: Any) -> None:
    """The containers, their images, the routes and the UI config of a conforming stack."""
    containers: list[str] = []
    for name, ids in STACK.items():
        for container in ids:
            containers.append(container)
            (stub / f"info-{container}").write_text(
                f"contractum/{name}:dev id-{name}\n", encoding="utf-8"
            )
    containers.append("c9")
    (stub / "info-c9").write_text("postgres:18 id-postgres\n", encoding="utf-8")
    containers.append("c10")
    (stub / "info-c10").write_text("contractum/e2e:dev id-e2e\n", encoding="utf-8")
    (stub / "containers").write_text("\n".join(containers) + "\n", encoding="utf-8")
    (stub / "env-c6").write_text(f"PATH=/bin\nDC_VERSION={VERSION}\n", encoding="utf-8")
    (stub / "ui-config.json").write_text(json.dumps({"version": VERSION}), encoding="utf-8")
    for module in ("identity", "contracts", "validation", "audit", "docs"):
        write_json(stub / f"route-{module}.json", {"version": VERSION, "commit": GIT_SHA})
    (stub / "tag-digest").write_text(digest("identity"), encoding="utf-8")
    for name in IMAGES:
        (stub / f"tag-{name}").write_text(digest(name), encoding="utf-8")
    for filename, content in changes.items():
        (stub / filename.replace("__", "-")).write_text(content, encoding="utf-8")


def script(bench: Path, argv: str, **env: str) -> subprocess.CompletedProcess[str]:
    stub = bench / "stub"
    log = bench / "log.txt"
    log.unlink(missing_ok=True)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "GITHUB"))}
    environment.update(
        PATH=f"{stub / 'bin'}{os.pathsep}{os.environ['PATH']}",
        STUB_LOG=str(log),
        STUB_DIR=str(stub),
        STUB_HEAD=COMMIT,
        DC_RELEASE_IMAGES=str(bench / "tested-images.json"),
        GITHUB_REPOSITORY="acme/contractum",
        TEST_ARGS=argv,
    )
    environment.update(env)
    return subprocess.run(
        ["/bin/bash", "-c", "exec bash scripts/release/tested_images.sh $TEST_ARGS"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def logged(bench: Path) -> list[str]:
    log = bench / "log.txt"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_the_script_refuses_without_a_manifest(bench: Path) -> None:
    unset = script(bench, "verify-stack", DC_RELEASE_IMAGES="")
    assert unset.returncode == 1
    assert "DC_RELEASE_IMAGES is not set" in unset.stderr
    missing = script(bench, "verify-stack", DC_RELEASE_IMAGES=str(bench / "nope.json"))
    assert missing.returncode == 1
    assert not logged(bench)


def test_the_script_refuses_a_malformed_manifest_before_any_docker_call(bench: Path) -> None:
    data = manifest_data(tested=True)
    data["images"]["ui"]["digest"] = "latest"
    write_json(bench / "tested-images.json", data)
    done = script(bench, "load")
    assert done.returncode != 0
    assert not [line for line in logged(bench) if line.startswith("docker")]


def test_an_unknown_command_is_refused(bench: Path) -> None:
    done = script(bench, "publish")
    assert done.returncode == 1
    assert "unknown command" in done.stderr


def test_load_pulls_every_digest_and_tags_it_for_compose(bench: Path) -> None:
    done = script(bench, "load")
    assert done.returncode == 0, done.stderr
    commands = logged(bench)
    for name in IMAGES:
        ref = f"{REGISTRY}/contractum/{name}@{digest(name)}"
        assert f"docker pull {ref}" in commands
        assert f"docker tag {ref} contractum/{name}:dev" in commands


def test_load_refuses_a_checkout_that_is_not_the_built_commit(bench: Path) -> None:
    done = script(bench, "load", STUB_HEAD="d" * 40)
    assert done.returncode == 1
    assert "images were built from" in done.stderr
    assert not [line for line in logged(bench) if line.startswith("docker")]


def test_load_refuses_a_pulled_image_without_the_exact_reference(bench: Path) -> None:
    done = script(bench, "load", STUB_NO_REPODIGEST="1")
    assert done.returncode == 1
    assert "does not hold the reference" in done.stderr
    assert not [line for line in logged(bench) if line.startswith("docker tag")]


def test_verify_tags_compares_every_tag_with_the_manifest_digest(bench: Path) -> None:
    ok = script(bench, f"verify-tags {VERSION}")
    assert ok.returncode == 0, ok.stderr
    for name in IMAGES:
        expected = f"docker buildx imagetools inspect {REGISTRY}/contractum/{name}:{VERSION}"
        assert any(line.startswith(expected) for line in logged(bench))
    write_stack(bench / "stub", tag__mail="sha256:" + "0" * 64)
    moved = script(bench, f"verify-tags {VERSION}")
    assert moved.returncode == 1
    assert "mail" in moved.stderr
    assert script(bench, "verify-tags").returncode == 1


def test_tag_retags_the_exact_digest_without_a_build_and_verifies_the_new_tag(bench: Path) -> None:
    done = script(bench, "tag 1.1.0")
    assert done.returncode == 0, done.stderr
    commands = logged(bench)
    for name in IMAGES:
        repository = f"{REGISTRY}/contractum/{name}"
        assert (
            f"docker buildx imagetools create --tag {repository}:1.1.0 {repository}@{digest(name)}"
            in commands
        )
    assert not [line for line in commands if line.startswith("docker build ")]
    assert not [line for line in commands if "buildx build" in line]
    inspected = [line for line in commands if "imagetools inspect" in line]
    assert len(inspected) == len(IMAGES)
    assert all(":1.1.0 " in line for line in inspected)


def test_tag_fails_when_the_new_tag_resolves_to_another_digest(bench: Path) -> None:
    write_stack(bench / "stub", tag__audit="sha256:" + "0" * 64)
    done = script(bench, "tag latest")
    assert done.returncode == 1
    assert "audit" in done.stderr
    assert "latest" in done.stderr
    assert script(bench, "tag").returncode == 1


ATTEST_FLAGS = (
    "--repo acme/contractum "
    "--signer-workflow acme/contractum/.github/workflows/release.yml "
    f"--source-digest {COMMIT} --source-ref refs/heads/main --deny-self-hosted-runners"
)


def test_verify_attestations_checks_the_manifest_and_all_seven_images_strictly(bench: Path) -> None:
    done = script(bench, "verify-attestations")
    assert done.returncode == 0, done.stderr
    commands = logged(bench)
    assert f"gh attestation verify {bench / 'tested-images.json'} {ATTEST_FLAGS}" in commands
    for name in IMAGES:
        ref = f"oci://{REGISTRY}/contractum/{name}@{digest(name)}"
        assert f"gh attestation verify {ref} {ATTEST_FLAGS}" in commands
    assert len(commands) == len(IMAGES) + 1


@pytest.mark.parametrize(
    "failing", ["tested-images.json", "contractum/ui@", "contractum/identity@"]
)
def test_verify_attestations_fails_when_any_one_fails(bench: Path, failing: str) -> None:
    done = script(bench, "verify-attestations", STUB_GH_FAIL=failing)
    assert done.returncode == 1
    assert "did not verify" in done.stderr


def test_verify_attestations_needs_the_repository(bench: Path) -> None:
    done = script(bench, "verify-attestations", GITHUB_REPOSITORY="")
    assert done.returncode != 0
    assert not logged(bench)


def test_verify_stack_accepts_a_stack_that_runs_the_release(bench: Path) -> None:
    done = script(bench, "verify-stack")
    assert done.returncode == 0, done.stderr
    assert f"stack runs the release {VERSION}" in done.stdout
    urls = [line for line in logged(bench) if line.startswith("curl")]
    assert len(urls) == 5
    assert all("--cacert .dev/ca.crt https://localhost:8443/api/v1/" in line for line in urls)
    assert any(line.endswith("/api/v1/docs/version") for line in urls)


def test_verify_stack_refuses_a_container_on_another_image_id(bench: Path) -> None:
    (bench / "stub" / "info-c3").write_text("contractum/contracts:dev id-other\n", encoding="utf-8")
    done = script(bench, "verify-stack")
    assert done.returncode == 1
    assert "contracts" in done.stderr
    assert "id-other" in done.stderr


def test_verify_stack_refuses_an_unknown_contractum_image(bench: Path) -> None:
    (bench / "stub" / "info-c9").write_text("contractum/extra:dev id-extra\n", encoding="utf-8")
    done = script(bench, "verify-stack")
    assert done.returncode == 1
    assert "not a release image" in done.stderr


def test_verify_stack_refuses_a_release_image_under_another_tag(bench: Path) -> None:
    (bench / "stub" / "info-c9").write_text("contractum/ui:latest id-ui\n", encoding="utf-8")
    done = script(bench, "verify-stack")
    assert done.returncode == 1
    assert "expected contractum/ui:dev" in done.stderr


def test_verify_stack_refuses_a_missing_component(bench: Path) -> None:
    containers = (bench / "stub" / "containers").read_text(encoding="utf-8").split()
    (bench / "stub" / "containers").write_text(
        "\n".join(c for c in containers if c != "c7") + "\n", encoding="utf-8"
    )
    done = script(bench, "verify-stack")
    assert done.returncode == 1
    assert "api-docs" in done.stderr


@pytest.mark.parametrize("module", ["identity", "contracts", "validation", "audit", "docs"])
def test_verify_stack_refuses_a_wrong_reported_version_or_commit(bench: Path, module: str) -> None:
    route = bench / "stub" / f"route-{module}.json"
    write_json(route, {"version": "dev", "commit": GIT_SHA})
    wrong_version = script(bench, "verify-stack")
    assert wrong_version.returncode == 1
    assert "reports version dev" in wrong_version.stderr
    write_json(route, {"version": VERSION, "commit": "dev"})
    wrong_commit = script(bench, "verify-stack")
    assert wrong_commit.returncode == 1
    assert "reports commit dev" in wrong_commit.stderr


def test_verify_stack_refuses_a_wrong_ui_or_mail_version(bench: Path) -> None:
    (bench / "stub" / "ui-config.json").write_text('{"version": "dev"}', encoding="utf-8")
    assert "config.json says version dev" in script(bench, "verify-stack").stderr
    write_stack(bench / "stub")
    (bench / "stub" / "env-c6").write_text("PATH=/bin\n", encoding="utf-8")
    done = script(bench, "verify-stack")
    assert done.returncode == 1
    assert "DC_VERSION is unset" in done.stderr
