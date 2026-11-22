"""The package imports lazily and reports its version."""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys

import pytest

import contractum


def test_version() -> None:
    assert contractum.__version__ == importlib.metadata.version("contractum")


def test_import_is_lazy() -> None:
    code = (
        "import sys, contractum; "
        "print(sorted(m for m in ('pyarrow', 'httpx', 'click') if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "[]"


def test_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError):
        contractum.does_not_exist  # noqa: B018


def test_error_export_is_the_errors_module_class() -> None:
    from contractum import errors

    assert contractum.ContractumError is errors.ContractumError


def test_assert_no_secret_reports_offset_not_secret(assert_no_secret: object) -> None:
    check = assert_no_secret  # type: ignore[misc]
    check("clean text", "hunter2")
    with pytest.raises(pytest.fail.Exception) as raised:
        check("prefix hunter2", "hunter2")
    assert "offset 7" in str(raised.value)
    assert "hunter2" not in str(raised.value)


def test_dir_lists_the_lazy_exports() -> None:
    names = dir(contractum)
    assert "Client" in names
    assert "ContractumError" in names
    assert names == sorted(names)
    assert contractum.__all__ == sorted(contractum.__all__)
