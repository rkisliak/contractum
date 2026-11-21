"""What the pytest plugin and the ``assert_conforms`` helper stand on.

``ContractResolver`` turns a contract reference into a ``ContractVersion`` once per pytest session
(fewer audited reads on the server). ``assert_conforms`` tests data against a contract and raises
``AssertionError`` with the engine-rendered, masked and bounded report text.

Nothing heavy is imported at module level: pyarrow, httpx and the client load only when a
fixture or ``assert_conforms`` is actually used, so an unrelated pytest run pays nothing. This
module does not import pytest either, so ``contractum.assert_conforms`` works without it.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from contractum.client import Client
    from contractum.contracts import ContractVersion
    from contractum.engine.masking import MaskMode
    from contractum.report.model import Report

DEFAULT_LOCK_NAME = "contractum.lock"
FROZEN_ENV = "CONTRACTUM_FROZEN"
_TRUE = frozenset({"1", "true", "yes", "on"})
_MAX_CHECKS = 20


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _under(base: Path, text: str) -> Path:
    path = Path(text)
    return path if path.is_absolute() else base / path


def lock_path_of(config: Any) -> Path:
    """The lock file named by ``--contractum-lock``, the ini value or the default.

    The command-line value is relative to the directory pytest was started in, the ini value to
    the rootdir.
    """
    given = config.getoption("contractum_lock")
    if given:
        return _under(Path(config.invocation_params.dir), str(given))
    return _under(Path(config.rootpath), str(config.getini("contractum_lock") or DEFAULT_LOCK_NAME))


class ContractResolver:
    """Resolve contract references for one pytest session, fetching each reference once."""

    def __init__(self, config: Any) -> None:
        self.frozen = bool(config.getoption("contractum_frozen")) or _env_flag(FROZEN_ENV)
        self.offline = bool(config.getoption("contractum_offline"))
        self.lock_path = lock_path_of(config)
        self._found: dict[str, ContractVersion] = {}
        self._client: Client | None = None

    @property
    def uses_lock(self) -> bool:
        """True when contracts come from the lock file instead of being resolved on the server."""
        return self.frozen or self.offline

    def client(self, fallback_url: str | None = None) -> Client:
        """The session's client, built on first use from ``CONTRACTUM_*`` and the profile file.

        ``fallback_url`` (the lock's server) fills in a URL nobody configured.
        """
        if self._client is None:
            from contractum.client import Client
            from contractum.errors import ConfigError

            try:
                self._client = Client.from_env()
            except ConfigError:
                if fallback_url is None:
                    raise
                self._client = Client.from_env(url=fallback_url)
        return self._client

    def get(self, ref: str) -> ContractVersion:
        """The contract version for ``ref``; the same object for the same reference."""
        found = self._found.get(ref)
        if found is None:
            found = self._found[ref] = self._load(ref)
        return found

    def close(self) -> None:
        """Close the connection pools of the client, if one was built."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def _load(self, ref: str) -> ContractVersion:
        if self.uses_lock:
            return self._from_lock(ref)
        return self.client().get(ref)

    def _from_lock(self, ref: str) -> ContractVersion:
        from contractum.cache import Cache
        from contractum.contracts import ContractDeprecationWarning
        from contractum.errors import FrozenLockError, OfflineError
        from contractum.lock import load_lock
        from contractum.pull import read_locked

        lock = load_lock(self.lock_path)
        entry = lock.find(ref)
        if entry is None:
            message = f"{ref!r} is not pinned in {self.lock_path.name}"
            hint = f"pin it first: contractum pull {ref}"
            if self.frozen:
                raise FrozenLockError(message, hint=hint)
            raise OfflineError(f"{message}, and offline mode cannot resolve it", hint=hint)
        client = None if self.offline else self.client(lock.server)
        found = read_locked(
            entry,
            lock_dir=self.lock_path.parent,
            cache=(client.cache if client is not None else None) or Cache(),
            client=client,
            server=lock.server,
        )
        notice = found.deprecation_notice
        if notice is not None:
            warnings.warn(notice, ContractDeprecationWarning, stacklevel=2)
        return found


def failure_text(report: Report, *, max_failures: int, max_checks: int = _MAX_CHECKS) -> str:
    """The report summary with the masked, bounded failing values under each check that has any.

    Every line of ``Report.summary_text`` is kept as it is; ``    row N: value`` lines follow the
    check they belong to. The values are the ones the engine already masked and bounded.
    """
    from contractum.report.text import sample_lines

    withheld = {
        f"{check.status} {check.id}": check for check in report.checks if check.status != "passed"
    }
    lines: list[str] = []
    for line in report.summary_text(max_checks=max_checks).split("\n"):
        lines.append(line)
        check = withheld.get(line.partition(": ")[0])
        if check is not None:
            lines.extend(f"    {sample}" for sample in sample_lines(check, max_failures))
    return "\n".join(lines)


def assert_conforms(
    data: object,
    contract: object,
    *,
    object: str | None = None,  # noqa: A002 - the public keyword reads naturally in tests
    max_failures: int = 5,
    mask: MaskMode = "auto",
    strict: bool = False,
    **options: Any,
) -> Report:
    """Assert that ``data`` conforms to ``contract``; return the report when it does.

    ``contract`` is a ``ContractVersion`` (from the ``contractum_contract`` fixture or
    ``Client.get``), a document mapping, YAML or JSON text or a contract file path; reference
    strings are not resolved here. ``object`` selects the schema object, ``max_failures`` bounds
    the failing values shown per check and ``mask`` is ``auto``, ``all`` or ``none``. A failed
    check fails the assertion; a warning only does with ``strict=True``. A skipped check never
    does. Other keyword options are those of ``TestOptions`` (``extra``, ``sample``, ``limit``...).

    The ``AssertionError`` message is the report summary with the failing values; values of
    restricted columns are masked by the engine before they get anywhere near it.
    """
    from contractum.testing import TestOptions, run_test

    opts = TestOptions(
        object_name=object, max_failures=max_failures, mask=mask, strict=strict, **options
    )
    report = run_test(contract, data, options=opts)
    if report.exit_code(strict=opts.strict, fail_on_skipped=opts.fail_on_skipped) != 0:
        raise AssertionError(failure_text(report, max_failures=opts.max_failures))
    return report


__all__ = ["ContractResolver", "assert_conforms", "failure_text", "lock_path_of"]
