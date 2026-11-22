"""See which file-lock guards exist while a write runs, the same way on every OS.

``filelock`` keeps the guard file after release on POSIX but deletes it on Windows, so a test
that looks for the guard after the call only proves a POSIX detail. These helpers record the
guards present at the moment the protected write happens, which is true on both.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest


def record_guards_during_writes(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, where: Callable[[Path], Path]
) -> list[list[str]]:
    """Wrap ``module.write_atomic``; before each write, record the ``*.lock`` names in ``where``.

    ``where`` maps the written path to the directory to list. One list of names is appended per
    write, so a caller can assert the guard was held for every write.
    """
    original = module.write_atomic
    seen: list[list[str]] = []

    def recording(path: str | os.PathLike[str], data: bytes, *, mode: int = 0o644) -> None:
        directory = where(Path(path))
        seen.append(sorted(p.name for p in directory.glob("*.lock")) if directory.is_dir() else [])
        original(path, data, mode=mode)

    monkeypatch.setattr(module, "write_atomic", recording)
    return seen
