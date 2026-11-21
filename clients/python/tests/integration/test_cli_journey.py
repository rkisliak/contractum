"""The ``contractum`` command as a person or a pipeline runs it, against the real stack.

Every command is a subprocess (``python -m contractum``) with the environment of the test and
absolute paths. The exit code is the verdict: 0 passes, 1 data does not match, 2 anything else.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET  # noqa: S405 - parses a report the test wrote itself
from pathlib import Path

import pytest
from support.integration import (
    World,
    bad_data,
    cli_env,
    good_data,
    preview_breaking_data,
    run_cli,
)

WRONG_TOKEN = "dcp_" + "0" * 16 + "_" + "W" * 43


@pytest.fixture
def pipeline_env(it_world: World, reader_pat: str) -> dict[str, str]:
    """The environment of a pipeline with the reader's PAT."""
    return cli_env(
        CONTRACTUM_URL=it_world.url,
        CONTRACTUM_CA_BUNDLE=str(it_world.ca),
        CONTRACTUM_TOKEN=reader_pat,
    )


def test_whoami_list_versions_show_and_pull(
    it_world: World, pipeline_env: dict[str, str], tmp_path: Path
) -> None:
    who = run_cli("whoami", "--json", env=pipeline_env)
    assert who.returncode == 0, who.stderr
    details = json.loads(who.stdout)
    assert details["via"] == "pat"
    assert details["read_only"] is True

    listed = run_cli("list", "--folder", it_world.folder_path, "--json", env=pipeline_env)
    assert listed.returncode == 0, listed.stderr
    assert [item["odcs_id"] for item in json.loads(listed.stdout)] == [it_world.odcs_id]

    versions = run_cli("versions", it_world.odcs_id, "--json", env=pipeline_env)
    assert versions.returncode == 0, versions.stderr
    assert [item["semver"] for item in json.loads(versions.stdout)] == ["2.0.0", "1.1.0", "1.0.0"]

    shown = run_cli("show", it_world.odcs_id, "--format", "yaml", env=pipeline_env)
    assert shown.returncode == 0, shown.stderr
    assert 'version: "1.1.0"' in shown.stdout or "version: 1.1.0" in shown.stdout
    metadata = run_cli("show", it_world.odcs_id, "--metadata", env=pipeline_env)
    assert metadata.returncode == 0, metadata.stderr

    pulled = run_cli(
        "pull",
        f"{it_world.odcs_id}@latest",
        "--dir",
        str(tmp_path / "contracts"),
        "--lock",
        str(tmp_path / "contractum.lock"),
        env=pipeline_env,
    )
    assert pulled.returncode == 0, pulled.stderr
    assert (tmp_path / "contractum.lock").is_file()


def test_exit_codes_zero_one_and_two(
    it_world: World, pipeline_env: dict[str, str], tmp_path: Path
) -> None:
    target = ["--contract", it_world.odcs_id]
    good = run_cli("test", str(good_data(tmp_path)), *target, env=pipeline_env)
    assert good.returncode == 0, good.stdout + good.stderr
    bad = run_cli("test", str(bad_data(tmp_path)), *target, env=pipeline_env)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    wrong = run_cli(
        "test",
        str(good_data(tmp_path)),
        *target,
        env={**pipeline_env, "CONTRACTUM_TOKEN": WRONG_TOKEN},
    )
    assert wrong.returncode == 2, wrong.stdout + wrong.stderr
    assert WRONG_TOKEN not in wrong.stdout + wrong.stderr


def test_the_preview_rejects_data_that_latest_accepts(
    it_world: World, pipeline_env: dict[str, str], tmp_path: Path
) -> None:
    data = str(preview_breaking_data(tmp_path))
    latest = run_cli("test", data, "--contract", it_world.odcs_id, env=pipeline_env)
    assert latest.returncode == 0, latest.stdout + latest.stderr
    preview = run_cli(
        "test", data, "--contract", it_world.odcs_id, "--version", "preview", env=pipeline_env
    )
    assert preview.returncode == 1, preview.stdout + preview.stderr
    compared = run_cli("compare", data, "--contract", it_world.odcs_id, env=pipeline_env)
    assert compared.returncode == 1, compared.stdout + compared.stderr


def test_json_to_stdout_and_a_junit_file(
    it_world: World, pipeline_env: dict[str, str], tmp_path: Path
) -> None:
    data = str(good_data(tmp_path))
    junit = tmp_path / "report.xml"
    result = run_cli(
        "test",
        data,
        "--contract",
        it_world.odcs_id,
        "--json",
        "-",
        "--junit",
        str(junit),
        env=pipeline_env,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["summary"]["failed"] == 0
    root = ET.parse(junit).getroot()  # noqa: S314
    assert root.tag in {"testsuites", "testsuite"}


def test_frozen_offline_test_needs_no_url_and_no_token(
    it_world: World, pipeline_env: dict[str, str], tmp_path: Path
) -> None:
    lock = str(tmp_path / "contractum.lock")
    pulled = run_cli(
        "pull",
        f"{it_world.odcs_id}@1.1.0",
        "--dir",
        str(tmp_path / "contracts"),
        "--lock",
        lock,
        env=pipeline_env,
    )
    assert pulled.returncode == 0, pulled.stderr
    offline_env = cli_env(CONTRACTUM_URL=None, CONTRACTUM_TOKEN=None, CONTRACTUM_CA_BUNDLE=None)
    result = run_cli(
        "test",
        str(good_data(tmp_path)),
        "--contract",
        it_world.odcs_id,
        "--frozen",
        "--offline",
        "--lock",
        lock,
        env=offline_env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_whoami_with_a_service_account_key_and_an_entra_token(
    it_world: World, sa_key: str, entra_token: str
) -> None:
    key_env = cli_env(
        CONTRACTUM_URL=it_world.url, CONTRACTUM_CA_BUNDLE=str(it_world.ca), CONTRACTUM_TOKEN=sa_key
    )
    key = run_cli("whoami", "--json", env=key_env)
    assert key.returncode == 0, key.stderr
    assert json.loads(key.stdout)["principal_type"] == "service_account"
    assert sa_key not in key.stdout + key.stderr

    entra = run_cli("whoami", "--json", env={**key_env, "CONTRACTUM_TOKEN": entra_token})
    assert entra.returncode == 0, entra.stderr
    assert json.loads(entra.stdout)["idp"] == "entra"
    assert entra_token not in entra.stdout + entra.stderr
