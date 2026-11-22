"""Keep pulled contract files and the lock byte-exact through Git with ``.gitattributes``.

The lock pins the SHA-256 of the exact bytes of each pulled file. Git's line-ending conversion
(``core.autocrlf=true``, the Git for Windows default) rewrites those bytes on checkout, and a
frozen or offline run then fails. ``-text`` turns every conversion off for a path, in both
directions and whatever ``core.autocrlf`` or ``core.eol`` say, so the checked-out bytes are the
committed bytes. ``eol=lf`` is not enough: it marks the path as text, so Git normalises a CRLF
that the server's own bytes may hold to LF on commit, and the hash no longer matches.

Lines are only ever appended: the user's own content is kept byte for byte and in order.
"""

from __future__ import annotations

import os
import re
import warnings
from collections.abc import Iterable
from pathlib import Path

from contractum._atomic import write_atomic

FILENAME = ".gitattributes"
ATTRIBUTE = "-text"
DEFAULT_MODE = 0o644
# A name that needs no quoting or escaping in a gitattributes pattern.
_PLAIN = re.compile(r"^[A-Za-z0-9._-]+$")


def _rule(pattern: str) -> str:
    return f"{pattern} {ATTRIBUTE}"


def _tokens(line: str) -> list[str]:
    return line.strip().split()


def ensure_lines(path: Path, rules: Iterable[str]) -> bool:
    """Append each rule that ``path`` does not hold yet; True when the file changed.

    A rule counts as present when a line has the same whitespace-separated tokens. New lines are
    written with LF; existing content is never rewritten, reordered or re-encoded.
    """
    try:
        existing = path.read_bytes()
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        existing, mode = b"", DEFAULT_MODE
    present = {
        tuple(_tokens(line)) for line in existing.decode("utf-8", errors="replace").splitlines()
    }
    missing = [rule for rule in dict.fromkeys(rules) if tuple(_tokens(rule)) not in present]
    if not missing:
        return False
    prefix = b"" if not existing or existing.endswith(b"\n") else b"\n"
    added = "".join(f"{rule}\n" for rule in missing).encode("utf-8")
    write_atomic(path, existing + prefix + added, mode=mode)
    return True


def _inside(child: Path, parent: Path) -> bool:
    return child != parent and child.is_relative_to(parent)


def _plan(directory: Path, lock_path: Path, files: Iterable[Path]) -> dict[Path, list[str]]:
    contracts = directory.resolve()
    lock_dir = lock_path.parent.resolve()
    plan: dict[Path, list[str]] = {}
    # ``*`` covers the whole contracts folder only when it is a subfolder of the lock's folder
    # and not a repository root; otherwise one anchored line per pulled file.
    if _inside(contracts, lock_dir) and not (contracts / ".git").exists():
        plan.setdefault(contracts / FILENAME, []).append(_rule("*"))
    else:
        for file in files:
            plan.setdefault(contracts / FILENAME, []).append(_rule(f"/{file.name}"))
    name = lock_path.name
    if _PLAIN.match(name):
        plan.setdefault(lock_dir / FILENAME, []).append(_rule(f"/{name}"))
    else:
        warnings.warn(
            f"cannot protect the lock file {name!r} with {FILENAME}: add a {ATTRIBUTE} line for it",
            UserWarning,
            stacklevel=3,
        )
    return plan


def protect(directory: str | os.PathLike[str], lock_path: Path, files: Iterable[Path]) -> None:
    """Ensure ``.gitattributes`` lines that keep the pulled ``files`` and the lock byte-exact.

    A file that cannot be read or written is a warning, never a failed pull: the contracts and
    the lock are already written and correct.
    """
    for target, rules in _plan(Path(directory), lock_path, files).items():
        try:
            ensure_lines(target, rules)
        except OSError as exc:
            warnings.warn(
                f"cannot update {target}: {type(exc).__name__}; add {ATTRIBUTE} lines yourself",
                UserWarning,
                stacklevel=2,
            )
