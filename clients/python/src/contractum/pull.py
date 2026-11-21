"""``pull``: write contract files and ``contractum.lock``, and read them back for frozen runs.

Normal mode resolves each reference on the server, writes the original bytes next to a lock
file that pins the exact version and hash. Frozen and offline modes never resolve ``latest`` or
``preview``: they read the pinned bytes from the pulled file or the cache, verify the lock's hash
and, online, check that the server still reports the same hash for that exact version.
"""

from __future__ import annotations

import hashlib
import os
import re
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from contractum._atomic import write_atomic
from contractum.cache import Cache, normalise_url
from contractum.contracts import (
    ContractDeprecationWarning,
    ContractVersion,
    LifecyclePolicy,
    evaluate_lifecycle,
)
from contractum.errors import FrozenLockError, OfflineError, ServerError, UsageError
from contractum.lock import (
    DEFAULT_LOCK,
    LockEntry,
    LockFile,
    load_lock,
    save_lock,
)
from contractum.refs import LATEST, parse_ref

if TYPE_CHECKING:
    from contractum.client import Client
    from contractum.engine.checks import CheckOutcome
    from contractum.models import VersionInfo

DEFAULT_DIRECTORY = "contracts"
MAX_ID_CHARS = 80
MAX_SEMVER_CHARS = 40
_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def _slug(text: str, limit: int) -> str:
    return _UNSAFE.sub("-", text.lower()).lstrip(".-_")[:limit]


def safe_filename(
    odcs_id: str | None, semver: str | None, contract_id: str, media_type: str
) -> str:
    """The file name of a pulled contract: ``<odcs-id>-<semver>-<id8>.<yaml|json>``.

    Every part is lower-cased and reduced to ``[a-z0-9._-]``; the name starts with a letter or
    digit, holds no separator, and a Windows device name is never the first dot-separated part.
    """
    stem = _slug(odcs_id or "", MAX_ID_CHARS) or "contract"
    if stem.split(".")[0] in _RESERVED:
        stem = f"id-{stem}"
    version = _slug(semver or "", MAX_SEMVER_CHARS) or "0"
    short = _slug(contract_id.replace("-", ""), 8) or "0"
    suffix = "json" if "json" in media_type.lower() else "yaml"
    return f"{stem}-{version}-{short}.{suffix}"


@dataclass(frozen=True)
class PulledContract:
    """One contract as pulled or read from the lock."""

    entry: LockEntry
    contract: ContractVersion
    path: Path
    lifecycle: CheckOutcome | None = None


@dataclass(frozen=True)
class PullResult:
    """What a pull did: the lock and the contracts it covers."""

    lock_path: Path
    lock: LockFile
    contracts: tuple[PulledContract, ...]

    @property
    def failed(self) -> bool:
        """True when a lifecycle policy (deprecated, sunset soon) rejected a contract."""
        return any(
            p.lifecycle is not None and p.lifecycle.status == "failed" for p in self.contracts
        )


def _inside(path: Path, *roots: Path) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def _relative(path: Path, start: Path) -> str:
    try:
        return Path(os.path.relpath(path, start)).as_posix()
    except ValueError:  # another drive on Windows
        return path.resolve().as_posix()


def _target(directory: Path, contract: ContractVersion) -> Path:
    name = safe_filename(
        contract.odcs_id, contract.semver, contract.contract_id or "", contract.media_type
    )
    target = directory / name
    if not _inside(target, directory):
        raise UsageError("refusing to write outside the target directory")
    return target


def _split(text: str) -> tuple[str, str]:
    parsed = parse_ref(text)
    return str(replace(parsed, version=None)), parsed.version or LATEST


def _entry(
    ref: str, requested: str, contract: ContractVersion, path: Path, lock_dir: Path
) -> LockEntry:
    if contract.contract_id is None or contract.semver is None:
        raise ServerError(200, title="the server sent no contract id or version")
    return LockEntry(
        ref=ref,
        requested=requested,
        contract_id=UUID(contract.contract_id),
        odcs_id=contract.odcs_id,
        folder_path=parse_ref(ref).folder_path,
        semver=contract.semver,
        content_sha256=contract.content_sha256,
        media_type=contract.media_type,
        file=_relative(path, lock_dir),
    )


def _existing(lock_path: Path) -> LockFile | None:
    return load_lock(lock_path) if lock_path.exists() else None


def _requests(refs: Sequence[str], lock: LockFile | None) -> list[tuple[str, str]]:
    if refs:
        return list(dict.fromkeys(_split(text) for text in refs))
    if lock is None or not lock.contracts:
        raise UsageError(
            "no references given and no lock to refresh",
            hint="name the contracts to pin: contractum pull payments.orders",
        )
    return [(entry.ref, entry.requested) for entry in lock.contracts]


def _finish(
    lock_path: Path,
    lock: LockFile,
    found: Sequence[tuple[LockEntry, ContractVersion, Path]],
    policy: LifecyclePolicy,
) -> PullResult:
    pulled: list[PulledContract] = []
    for entry, contract, path in found:
        notice = contract.deprecation_notice
        if notice is not None:
            warnings.warn(notice, ContractDeprecationWarning, stacklevel=3)
        pulled.append(PulledContract(entry, contract, path, evaluate_lifecycle(contract, policy)))
    return PullResult(lock_path, lock, tuple(pulled))


def _resolve_all(
    client: Client, lock_path: Path, directory: Path, refs: Sequence[str], store: Cache
) -> tuple[LockFile, list[tuple[LockEntry, ContractVersion, Path]]]:
    lock = _existing(lock_path)
    server = normalise_url(client.url)
    if lock is not None and lock.contracts and normalise_url(lock.server) != server:
        raise UsageError(
            f"{lock_path.name} pins contracts of {lock.server}, not of {server}",
            hint="use another --lock file for this server",
        )
    entries = {e.key(): e for e in lock.contracts} if lock else {}
    found: list[tuple[LockEntry, ContractVersion, Path]] = []
    for ref, requested in _requests(refs, lock):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ContractDeprecationWarning)
            contract = client.get(ref, version=requested)
        path = _target(directory, contract)
        write_atomic(path, contract.content)
        entry = _entry(ref, requested, contract, path, lock_path.parent.absolute())
        entries[entry.key()] = entry
        found.append((entry, contract, path))
    return LockFile(server=server, contracts=list(entries.values())), found


def _verify_bytes(entry: LockEntry, data: bytes, what: str) -> bytes:
    if hashlib.sha256(data).hexdigest() != entry.content_sha256:
        raise FrozenLockError(
            f"{what} for {entry.label} does not match the lock (content hash differs)",
            hint="restore the file, or run `contractum pull` to re-pin deliberately",
        )
    return data


def _cached(cache: Cache, server: str | None, entry: LockEntry) -> ContractVersion | None:
    if server is None:
        return None
    try:
        return cache.get(server, str(entry.contract_id), entry.semver)
    except OSError:
        return None


def _verified_info(entry: LockEntry, client: Client | None) -> VersionInfo | None:
    if client is None:
        return None
    info = client.version_metadata(entry.contract_id, entry.semver)
    if info.semver.partition("+")[0] != entry.semver.partition("+")[0]:
        raise FrozenLockError(f"the server answered version {info.semver} for {entry.label}")
    if info.content_sha256 != entry.content_sha256:
        raise FrozenLockError(
            f"the server's {entry.label} differs from the lock (content hash changed)",
            hint="the pinned version no longer matches the server; investigate before re-pinning",
        )
    return info


def _locked_bytes(
    entry: LockEntry, lock_dir: Path, cached: ContractVersion | None, client: Client | None
) -> bytes:
    try:
        data: bytes | None = (lock_dir / entry.file).read_bytes()
    except OSError:
        data = None
    if data is not None:
        return _verify_bytes(entry, data, "the pulled file")
    if cached is not None:
        return _verify_bytes(entry, cached.content, "the cached copy")
    if client is None:
        raise OfflineError(
            f"{entry.label} is neither in {entry.file} nor in the cache",
            hint="run `contractum pull` online once, or restore the file",
        )
    fetched = client.fetch_document(entry.contract_id, entry.semver)
    if fetched.content is None:
        raise ServerError(fetched.status, title="the server sent no document")
    return _verify_bytes(entry, fetched.content, "the server's document")


def read_locked(
    entry: LockEntry,
    *,
    lock_dir: str | os.PathLike[str],
    cache: Cache,
    client: Client | None,
    server: str | None = None,
) -> ContractVersion:
    """The exact bytes a lock entry pins, verified against its hash; never another version.

    The pulled file is read first, then the cache, then (when ``client`` is given) the server by
    exact SemVer. A file or cache copy that does not hash to the lock is a ``FrozenLockError``.
    With a client the server's metadata hash for that version must equal the lock too. Without
    a client nothing touches the network and missing bytes raise ``OfflineError``. ``server`` is
    the lock's server URL, used to find the cache entry when there is no client.
    """
    info = _verified_info(entry, client)
    cached = _cached(cache, client.url if client is not None else server, entry)
    if cached is not None and cached.content_sha256 != entry.content_sha256:
        raise FrozenLockError(
            f"the cached copy of {entry.label} does not match the lock (content hash differs)"
        )
    content = _locked_bytes(entry, Path(lock_dir), cached, client)
    meta: VersionInfo | ContractVersion | None = info or cached
    return ContractVersion(
        contract_id=str(entry.contract_id),
        odcs_id=entry.odcs_id,
        name=cached.name if cached is not None else None,
        semver=entry.semver,
        status=meta.status if meta is not None else None,
        media_type=entry.media_type,
        content=content,
        content_sha256=entry.content_sha256,
        etag=f'"{entry.content_sha256}"',
        deprecated=meta.deprecated if meta is not None else False,
        deprecated_at=meta.deprecated_at if meta is not None else None,
        sunset_date=meta.sunset_date if meta is not None else None,
        published_at=meta.published_at if meta is not None else None,
        source="lock",
    )


def _locked_entries(lock: LockFile, refs: Sequence[str], *, frozen: bool) -> list[LockEntry]:
    if not refs:
        return list(lock.contracts)
    entries: list[LockEntry] = []
    for text in refs:
        found = lock.find(text)
        if found is None:
            message = f"{text!r} is not pinned in the lock"
            if frozen:
                raise FrozenLockError(message, hint="pin it first: contractum pull " + text)
            raise OfflineError(
                message + ", and offline mode cannot resolve it",
                hint="pin it first: contractum pull " + text,
            )
        entries.append(found)
    return entries


def _read_locked_all(
    client: Client | None,
    lock_path: Path,
    directory: Path,
    refs: Sequence[str],
    frozen: bool,
    store: Cache,
) -> tuple[LockFile, list[tuple[LockEntry, ContractVersion, Path]]]:
    lock = load_lock(lock_path)
    lock_dir = lock_path.parent.absolute()
    found: list[tuple[LockEntry, ContractVersion, Path]] = []
    for entry in _locked_entries(lock, refs, frozen=frozen):
        contract = read_locked(
            entry, lock_dir=lock_dir, cache=store, client=client, server=lock.server
        )
        path = lock_dir / entry.file
        if not path.exists() and _inside(path, directory, lock_dir):
            write_atomic(path, contract.content)
        found.append((entry, contract, path))
    return lock, found


def pull(
    client: Client | None,
    refs: Sequence[str] = (),
    *,
    directory: str | os.PathLike[str] = DEFAULT_DIRECTORY,
    lock_path: str | os.PathLike[str] = DEFAULT_LOCK,
    frozen: bool = False,
    offline: bool = False,
    policy: LifecyclePolicy | None = None,
    cache: Cache | None = None,
) -> PullResult:
    """Pin contracts into ``directory`` and ``lock_path``, or verify and restore a pinned set.

    Normal mode resolves each reference (the lock's own references when none are given), writes
    the files and then the lock. ``frozen`` and ``offline`` read the lock instead and never
    resolve ``latest`` or ``preview``; ``offline`` also needs no client. A lifecycle policy hit
    is reported in the result, after the files and the lock are written.
    """
    target_dir = Path(directory)
    lock_file = Path(lock_path)
    chosen = policy if policy is not None else LifecyclePolicy()
    if offline:
        client = None
    store = cache
    if store is None:
        store = client.cache if client is not None and client.cache is not None else Cache()
    if frozen or offline:
        lock, found = _read_locked_all(client, lock_file, target_dir, refs, frozen, store)
    elif client is None:
        raise UsageError("pull needs a client unless it runs offline")
    else:
        lock, found = _resolve_all(client, lock_file, target_dir, refs, store)
        save_lock(lock_file, lock, cache_dir=store.root)
    return _finish(lock_file, lock, found, chosen)
