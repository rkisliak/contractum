"""``contractum.lock``: the committed record of which contract versions a project is pinned to.

The file is canonical JSON (sorted keys, two-space indent, one trailing newline) and holds no
timestamps, so pulling the same versions twice leaves it byte-identical. ``lock_version`` is 1.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contractum._atomic import write_atomic
from contractum.cache import LOCK_TIMEOUT_SECONDS, cache_root
from contractum.errors import FrozenLockError, UsageError
from contractum.refs import LATEST, PREVIEW, parse_ref

LOCK_VERSION = 1
DEFAULT_LOCK = "contractum.lock"
_SHA256 = r"^[0-9a-f]{64}$"


class LockEntry(BaseModel):
    """One pinned contract version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    """The reference as the user typed it, without a version suffix."""
    requested: str
    """``latest``, ``preview`` or the exact SemVer the user asked for."""
    contract_id: UUID
    odcs_id: str | None = None
    folder_path: str | None = None
    semver: str
    content_sha256: str = Field(pattern=_SHA256)
    media_type: str
    file: str
    """The pulled file, relative to the directory of the lock file (forward slashes)."""

    @property
    def label(self) -> str:
        """``<odcs-id or ref>@<semver>``, for messages."""
        return f"{self.odcs_id or self.ref}@{self.semver}"

    def key(self) -> tuple[str, str]:
        """What makes an entry unique: the reference and the version asked for."""
        return (self.ref, self.requested)


class LockFile(BaseModel):
    """The whole lock file."""

    model_config = ConfigDict(extra="forbid")

    lock_version: int = LOCK_VERSION
    server: str
    contracts: list[LockEntry] = Field(default_factory=list)

    def find(self, ident: str, version: str | None = None) -> LockEntry | None:
        """The entry pinning ``ident`` (a reference, ODCS id or contract id), or None.

        ``version`` is an exact SemVer (it must equal the pinned one) or ``latest``/``preview``
        (it must equal what was requested when pulling). Without it, the entry is returned when
        only one is pinned; several pinned versions of one contract are an error.
        """
        parsed = parse_ref(ident)
        wanted = version if version is not None else parsed.version
        hits = [e for e in self.contracts if _names(e, parsed.folder_path, parsed.ident)]
        if wanted is not None:
            hits = [e for e in hits if _has_version(e, wanted)]
        if len(hits) > 1:
            pinned = ", ".join(sorted(e.semver for e in hits))
            raise FrozenLockError(
                f"the lock pins several versions of {ident!r} ({pinned})",
                hint="choose one with --version",
            )
        return hits[0] if hits else None


def _names(entry: LockEntry, folder_path: str | None, ident: str) -> bool:
    if folder_path is None:
        return ident in (entry.ref, entry.odcs_id, str(entry.contract_id))
    return entry.ref == f"{folder_path}:{ident}" or (
        entry.folder_path == folder_path and entry.odcs_id == ident
    )


def _has_version(entry: LockEntry, wanted: str) -> bool:
    if wanted in (LATEST, PREVIEW):
        return entry.requested == wanted
    return entry.semver.partition("+")[0] == wanted.partition("+")[0]


def load_lock(path: str | os.PathLike[str]) -> LockFile:
    """Read a lock file; a missing, malformed or newer-format file is a ``UsageError``."""
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise UsageError(
            f"cannot read the lock file {target.name}: {type(exc).__name__}",
            hint="create it with `contractum pull REF...`",
        ) from exc
    try:
        data: Any = json.loads(raw)
    except ValueError as exc:
        raise UsageError(f"{target.name} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise UsageError(f"{target.name} is not a lock file")
    if data.get("lock_version") != LOCK_VERSION:
        raise UsageError(
            f"{target.name} has an unsupported lock_version",
            hint=f"this contractum reads lock_version {LOCK_VERSION}; upgrade contractum",
        )
    try:
        return LockFile.model_validate(data)
    except ValidationError as exc:
        fields = ", ".join(".".join(str(part) for part in e["loc"]) for e in exc.errors())
        raise UsageError(f"{target.name} is malformed ({fields})") from exc


def render_lock(lock: LockFile) -> bytes:
    """The canonical bytes of a lock file; entries are sorted so the output is stable."""
    ordered = sorted(
        lock.contracts, key=lambda e: (e.ref, str(e.contract_id), e.requested, e.semver)
    )
    body = LockFile(server=lock.server, contracts=ordered).model_dump(mode="json")
    return (json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def save_lock(
    path: str | os.PathLike[str], lock: LockFile, *, cache_dir: str | os.PathLike[str] | None = None
) -> None:
    """Write the lock file atomically under a file lock kept in the cache directory.

    The lock artefact is ``<cache>/locks/<sha256 of the absolute lock path>.lock``, so nothing
    but the lock file itself lands in the user's repository. On any failure the previous lock
    file is untouched.
    """
    target = Path(path).absolute()
    root = Path(cache_dir) if cache_dir is not None else cache_root()
    guard = root / "locks" / (hashlib.sha256(str(target).encode("utf-8")).hexdigest() + ".lock")
    guard.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(guard), timeout=LOCK_TIMEOUT_SECONDS):
        write_atomic(target, render_lock(lock))
