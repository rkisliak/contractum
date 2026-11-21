"""The single atomic file writer used by every module that persists data."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomic(path: str | os.PathLike[str], data: bytes, *, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` so a reader sees the old or the new file, never a mix.

    The bytes go to a private temp file in the same directory (created ``0600`` with ``O_EXCL``),
    which gets ``mode``, is flushed to disk and replaces the target. The parent directory is
    created when missing. On any failure the temp file is removed and the target is untouched.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            else:
                os.chmod(temp, mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
