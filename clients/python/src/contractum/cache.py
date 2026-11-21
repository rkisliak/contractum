"""The on-disk contract cache: byte-faithful documents revalidated with ``If-None-Match``.

Layout: ``<root>/v1/<server-key>/<contract-id>/<semver>/{document,meta.json}``. The server key is
the first 16 hex characters of the SHA-256 of the normalised server URL, so two servers never
share an entry. Every entry is written under a file lock with ``write_atomic`` and read back
only when its document still hashes to the value recorded in ``meta.json``; anything else is
discarded and fetched again.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import platformdirs
from filelock import FileLock
from pydantic import BaseModel, ConfigDict

from contractum._atomic import write_atomic
from contractum.contracts import ContractVersion

ENV_CACHE_DIR = "CONTRACTUM_CACHE_DIR"
LAYOUT = "v1"
LOCK_TIMEOUT_SECONDS = 30.0
_SAFE_PART = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]*")
_DOCUMENT = "document"
_META = "meta.json"
_LOCK = ".lock"


def cache_root(env: Mapping[str, str] | None = None) -> Path:
    """The cache directory: ``CONTRACTUM_CACHE_DIR`` or the platform user cache directory."""
    environ = os.environ if env is None else env
    configured = environ.get(ENV_CACHE_DIR, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(platformdirs.user_cache_dir("contractum"))


def normalise_url(url: str) -> str:
    """Lower-case scheme and host and drop a trailing slash, so equal servers compare equal."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def server_key(url: str) -> str:
    """The short, path-safe key of a server URL."""
    return hashlib.sha256(normalise_url(url).encode("utf-8")).hexdigest()[:16]


def _part(text: str) -> str:
    """A path-safe directory name for a server-supplied identifier."""
    if _SAFE_PART.fullmatch(text):
        return text
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class _Meta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content_sha256: str
    etag: str | None = None
    media_type: str
    contract_id: str | None = None
    odcs_id: str | None = None
    name: str | None = None
    semver: str | None = None
    status: str | None = None
    deprecated: bool = False
    deprecated_at: datetime | None = None
    sunset_date: date | None = None
    published_at: datetime | None = None

    @classmethod
    def of(cls, contract: ContractVersion) -> _Meta:
        return cls(
            content_sha256=contract.content_sha256,
            etag=contract.etag,
            media_type=contract.media_type,
            contract_id=contract.contract_id,
            odcs_id=contract.odcs_id,
            name=contract.name,
            semver=contract.semver,
            status=contract.status,
            deprecated=contract.deprecated,
            deprecated_at=contract.deprecated_at,
            sunset_date=contract.sunset_date,
            published_at=contract.published_at,
        )

    def version(self, content: bytes) -> ContractVersion:
        return ContractVersion(
            contract_id=self.contract_id,
            odcs_id=self.odcs_id,
            name=self.name,
            semver=self.semver,
            status=self.status,
            media_type=self.media_type,
            content=content,
            content_sha256=self.content_sha256,
            etag=self.etag,
            deprecated=self.deprecated,
            deprecated_at=self.deprecated_at,
            sunset_date=self.sunset_date,
            published_at=self.published_at,
            source="cache",
        )


class Cache:
    """A directory of cached contract versions, safe for concurrent processes."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root) if root is not None else cache_root()

    def __repr__(self) -> str:
        return f"Cache(root={str(self.root)!r})"

    def entry_dir(self, server: str, contract_id: str, semver: str) -> Path:
        """The directory of one cached version."""
        return self.root / LAYOUT / server_key(server) / _part(contract_id) / _part(semver)

    @contextmanager
    def _locked(self, directory: Path) -> Iterator[None]:
        directory.mkdir(parents=True, exist_ok=True)
        with FileLock(str(directory / _LOCK), timeout=LOCK_TIMEOUT_SECONDS):
            yield

    def get(self, server: str, contract_id: str, semver: str) -> ContractVersion | None:
        """Return the cached version, or None when absent or when it fails its hash check."""
        directory = self.entry_dir(server, contract_id, semver)
        if not directory.is_dir():
            return None
        with self._locked(directory):
            return self._read(directory)

    def _read(self, directory: Path) -> ContractVersion | None:
        try:
            meta = _Meta.model_validate_json((directory / _META).read_bytes())
            content = (directory / _DOCUMENT).read_bytes()
        except (OSError, ValueError):
            self._discard(directory)
            return None
        if hashlib.sha256(content).hexdigest() != meta.content_sha256:
            self._discard(directory)
            return None
        return meta.version(content)

    @staticmethod
    def _discard(directory: Path) -> None:
        for name in (_META, _DOCUMENT):
            with suppress(OSError):
                (directory / name).unlink(missing_ok=True)

    def put(self, server: str, contract: ContractVersion) -> None:
        """Store a version (document first, then ``meta.json``) under the entry's file lock."""
        if contract.contract_id is None or contract.semver is None:
            return
        directory = self.entry_dir(server, contract.contract_id, contract.semver)
        with self._locked(directory):
            write_atomic(directory / _DOCUMENT, contract.content, mode=0o600)
            write_atomic(
                directory / _META,
                _Meta.of(contract).model_dump_json(indent=2).encode("utf-8") + b"\n",
                mode=0o600,
            )
