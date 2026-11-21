"""Vendored schema integrity (T-01-11): files match the manifest, the manifest matches the gate."""

from __future__ import annotations

import hashlib
import json
import re
from importlib import resources

import pytest
from dc_odcs import SUPPORTED_API_VERSIONS

pytestmark = pytest.mark.unit

UPSTREAM_REPO = "bitol-io/open-data-contract-standard"
PINNED_COMMIT = "04d1bfd2fbbb3b3f38c8f9540d86524ce8a8abe2"
EXPECTED_SHA256 = {
    "v3.2.0": "edb41f33ec46e84780e99872ab2bd67f074959d2bf3e9c9fc54e61f8982b0d93",
    "v3.1.0": "f99a3093a5d65326837f96278654067ffdc10de314fc1f45b9dbfeb1d3429f84",
}
# The as-tagged v3.1.0 file (research, Pattern 6). D-20 vendors the amended main copy instead.
AS_TAGGED_V310_SHA256 = "2cb7dd6fe43344d2233e0406438622681dc3ebadcf8f0d606a15b40c8f6752c0"


def _schema_dir() -> resources.abc.Traversable:
    return resources.files("dc_odcs") / "schemas"


def _manifest() -> list[dict[str, str]]:
    return json.loads((_schema_dir() / "MANIFEST.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_every_vendored_file_matches_its_manifest_hash() -> None:
    rows = _manifest()
    assert rows
    for row in rows:
        digest = hashlib.sha256((_schema_dir() / row["file"]).read_bytes()).hexdigest()
        assert digest == row["sha256"], f"{row['file']} drifted from MANIFEST.json"


def test_manifest_rows_are_pinned_to_one_upstream_commit_and_the_expected_hashes() -> None:
    rows = {row["api_version"]: row for row in _manifest()}
    assert {row["upstream_repo"] for row in rows.values()} == {UPSTREAM_REPO}
    assert {row["commit"] for row in rows.values()} == {PINNED_COMMIT}
    assert {version: row["sha256"] for version, row in rows.items()} == EXPECTED_SHA256
    for version, row in rows.items():
        assert row["source_path"] == f"schema/odcs-json-schema-{version}.json"
        assert row["file"] == f"odcs-json-schema-{version}.json"


def test_every_supported_api_version_has_a_manifest_row() -> None:
    assert {row["api_version"] for row in _manifest()} == set(SUPPORTED_API_VERSIONS)


def test_v310_is_the_amended_main_copy_not_the_as_tagged_file() -> None:
    row = next(row for row in _manifest() if row["api_version"] == "v3.1.0")
    assert row["sha256"] != AS_TAGGED_V310_SHA256


def test_schemas_target_draft_2019_09() -> None:
    for row in _manifest():
        schema = json.loads((_schema_dir() / row["file"]).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2019-09/schema"


def test_required_top_level_fields_per_version() -> None:
    required = {}
    for row in _manifest():
        schema = json.loads((_schema_dir() / row["file"]).read_text(encoding="utf-8"))
        required[row["api_version"]] = set(schema["required"])
    assert required["v3.2.0"] == {"version", "apiVersion", "kind", "id"}
    assert required["v3.1.0"] == {"version", "apiVersion", "kind", "id", "status"}


def test_no_moving_target_schema_url_is_referenced() -> None:
    manifest_text = (_schema_dir() / "MANIFEST.json").read_text(encoding="utf-8")
    assert "latest" not in manifest_text


def test_schema_manifest_sha256_is_the_hash_of_the_manifest_file_and_stable() -> None:
    from dc_odcs import schema_manifest_sha256

    expected = hashlib.sha256((_schema_dir() / "MANIFEST.json").read_bytes()).hexdigest()

    first = schema_manifest_sha256()
    second = schema_manifest_sha256()

    assert first == second == expected
    assert re.fullmatch(r"[0-9a-f]{64}", first)
