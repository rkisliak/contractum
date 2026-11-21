"""Where ``test`` and ``compare`` get their contract: the server, a lock file or a local file."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import click

from contractum.cli import output
from contractum.cli.context import get_options, settings_from_ctx
from contractum.errors import ConfigError, FrozenLockError, OfflineError, UsageError
from contractum.lock import DEFAULT_LOCK, LockEntry, LockFile, load_lock

if TYPE_CHECKING:
    from contractum.client import Client
    from contractum.contracts import ContractVersion

F = TypeVar("F", bound=Callable[..., Any])


def contract_source_options(f: F) -> F:
    """Add the contract-source options: ``--contract`` or ``--contract-file`` and how to resolve it.

    The values arrive as the keyword arguments ``contract``, ``contract_file``, ``version``,
    ``lock_path``, ``frozen`` and ``offline``; pass them to ``resolve_contract``.
    """
    options = [
        click.option(
            "--offline",
            "offline",
            is_flag=True,
            default=False,
            help="Use only the lock, the pulled files and the cache; no network.",
        ),
        click.option(
            "--frozen",
            "frozen",
            is_flag=True,
            default=False,
            envvar="CONTRACTUM_FROZEN",
            help="Use the version pinned in the lock; never resolve latest or preview.",
        ),
        click.option(
            "--lock",
            "lock_path",
            type=click.Path(dir_okay=False, path_type=Path),
            default=Path(DEFAULT_LOCK),
            show_default=True,
            help="The lock file read by --frozen and --offline.",
        ),
        click.option(
            "--version",
            "version",
            metavar="VERSION",
            default=None,
            help="latest, preview or an exact SemVer; overrides the version in --contract.",
        ),
        click.option(
            "--contract-file",
            "contract_file",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            default=None,
            help="Use a local ODCS contract file; no server needed.",
        ),
        click.option(
            "--contract",
            "contract",
            metavar="REF",
            default=None,
            help="The contract: [FOLDER-PATH:]ODCS-ID-OR-UUID[@latest|@preview|@X.Y.Z].",
        ),
    ]
    for option in options:
        f = option(f)
    return f


def lock_server(path: Path) -> str | None:
    """The server a lock file names, or None when it cannot be read (the caller reports that)."""
    try:
        return load_lock(path).server
    except UsageError:
        return None


def client_with_fallback(ctx: click.Context, fallback_url: str | None = None) -> Client:
    """Build the client for this run; ``fallback_url`` (the lock's server) fills a missing URL."""
    from contractum.client import Client
    from contractum.config import Overrides, resolve_settings

    try:
        settings = settings_from_ctx(ctx)
    except ConfigError:
        if fallback_url is None:
            raise
        options = get_options(ctx)
        settings = resolve_settings(
            profile=options.profile,
            overrides=Overrides(
                url=fallback_url,
                ca_bundle=options.ca_bundle,
                allow_insecure_http=True if options.allow_insecure_http else None,
            ),
        )
    return Client.from_settings(settings)


def _pinned(lock: LockFile, ref: str, version: str | None, *, frozen: bool) -> LockEntry:
    from dataclasses import replace

    from contractum.refs import LATEST, parse_ref

    entry = lock.find(ref, version)
    if entry is not None:
        return entry
    parsed = parse_ref(ref)
    bare = str(replace(parsed, version=None))
    other = lock.find(bare)
    if other is not None:
        asked = version or parsed.version or LATEST
        raise FrozenLockError(
            f"the lock pins {other.label}, not {asked}",
            hint="pin the version you need: contractum pull " + ref,
        )
    message = f"{ref!r} is not pinned in the lock"
    if frozen:
        raise FrozenLockError(message, hint="pin it first: contractum pull " + ref)
    raise OfflineError(
        message + ", and offline mode cannot resolve it",
        hint="pin it first: contractum pull " + ref,
    )


def _from_lock(
    ctx: click.Context,
    ref: str,
    version: str | None,
    lock_path: Path,
    *,
    frozen: bool,
    offline: bool,
) -> ContractVersion:
    from contractum.cache import Cache
    from contractum.pull import read_locked

    lock = load_lock(lock_path)
    entry = _pinned(lock, ref, version, frozen=frozen)
    if offline:
        return read_locked(
            entry, lock_dir=lock_path.parent, cache=Cache(), client=None, server=lock.server
        )
    with client_with_fallback(ctx, lock.server) as client:
        return read_locked(
            entry,
            lock_dir=lock_path.parent,
            cache=client.cache or Cache(),
            client=client,
            server=lock.server,
        )


def _from_server(ctx: click.Context, ref: str, version: str | None) -> ContractVersion:
    with client_with_fallback(ctx) as client, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        contract = client.get(ref, version=version)
    for item in caught:
        output.warn(str(item.message))
    return contract


def resolve_contract(
    ctx: click.Context,
    *,
    contract: str | None = None,
    contract_file: Path | None = None,
    version: str | None = None,
    lock_path: Path = Path(DEFAULT_LOCK),
    frozen: bool = False,
    offline: bool = False,
) -> ContractVersion:
    """The contract version a command runs against, from a file, the lock or the server.

    Exactly one of ``contract`` and ``contract_file`` is required. A file needs no server.
    ``frozen`` and ``offline`` read the pinned version from the lock (``frozen`` verifies it
    against the server unless ``offline``; without a configured URL the lock's server is used).
    Without them the reference is resolved on the server. A deprecated version prints a warning.
    """
    from contractum.contracts import ContractVersion

    if (contract is None) == (contract_file is None):
        raise UsageError(
            "pass exactly one of --contract and --contract-file",
            hint="--contract REF fetches from Contractum; --contract-file PATH reads a local file",
        )
    if contract_file is not None:
        if version is not None:
            raise UsageError("--version applies to --contract, not to --contract-file")
        return ContractVersion.from_file(contract_file)
    ref = contract or ""
    if frozen or offline:
        found = _from_lock(ctx, ref, version, lock_path, frozen=frozen, offline=offline)
        notice = found.deprecation_notice
        if notice is not None:
            output.warn(notice)
        return found
    return _from_server(ctx, ref, version)
