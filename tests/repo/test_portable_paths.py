"""Every tracked path can be checked out on Windows, macOS and Linux.

Windows refuses file and folder names that are reserved device names (CON, PRN, AUX, NUL, COM1 to
COM9, LPT1 to LPT9, with or without an extension), names that end in a dot or a space, and the
characters ``<>:"|?*``. A single such path makes ``git clone`` fail there (``invalid path``), so
nobody on Windows could clone the repository and the Windows CI job could not start. macOS and
Windows also treat names that differ only in case as the same file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
FORBIDDEN_CHARS = set('<>:"|?*\\')


def problems(paths: list[str]) -> list[str]:
    """Each path component Windows cannot create, plus case-only clashes, sorted."""
    found: list[str] = []
    for path in paths:
        for part in path.split("/"):
            if RESERVED.match(part):
                found.append(f"{path}: '{part}' is a reserved device name on Windows")
            elif part.endswith((".", " ")):
                found.append(f"{path}: '{part}' ends in a dot or a space")
            elif FORBIDDEN_CHARS & set(part) or any(ord(c) < 32 for c in part):
                found.append(f"{path}: '{part}' has a character Windows does not allow")
    seen: dict[str, str] = {}
    for path in paths:
        key = path.lower()
        if key in seen and seen[key] != path:
            found.append(f"{path}: differs only in case from {seen[key]}")
        seen.setdefault(key, path)
    return sorted(found)


def test_tracked_paths_are_portable() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    paths = [p for p in listed.split("\0") if p]
    assert problems(paths) == []


def test_reserved_and_bad_names_are_reported() -> None:
    assert problems(["a/nul.py", "b/CON", "c/Aux.txt", "d/com1.md", "e/lpt9"]) == [
        "a/nul.py: 'nul.py' is a reserved device name on Windows",
        "b/CON: 'CON' is a reserved device name on Windows",
        "c/Aux.txt: 'Aux.txt' is a reserved device name on Windows",
        "d/com1.md: 'com1.md' is a reserved device name on Windows",
        "e/lpt9: 'lpt9' is a reserved device name on Windows",
    ]
    assert problems(["x/name.", "y/a:b.py", "z/Readme.md", "z/README.md"]) == [
        "x/name.: 'name.' ends in a dot or a space",
        "y/a:b.py: 'a:b.py' has a character Windows does not allow",
        "z/README.md: differs only in case from z/Readme.md",
    ]


def test_ordinary_names_pass() -> None:
    assert problems(["shared/dc-core/src/dc_core/nul_guard.py", "console.py", "nullable.py"]) == []
