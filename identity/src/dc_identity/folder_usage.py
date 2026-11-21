"""What the other services still hold for a folder, and the purge of what may go with it.

Deleting a folder (quick 261119-0v1) needs two answers identity cannot give from its own database:
which contracts, templates, subscriber cards and rule sets still sit in the folder, and a
permanent removal of the leftovers that no longer matter. ``FolderUsagePort`` is that seam;
``HttpFolderUsage`` calls the internal usage and purge routes of contracts and validation with the
**caller's own bearer** (never a service credential), so each service authorises ``folder:delete``
itself (no confused deputy, T-5-40; the same pattern as contracts' ``ValidationClient``).

Fail closed: anything but a clean 200 with well-formed counts (a timeout, a connection error, a
5xx, a 403 or 404 that disagrees with identity's own decision, a malformed body, a count that is
negative or not an integer, a body about another folder) is 503 ``service-unavailable`` and the
caller deletes nothing. 401 stays the 401 of an expired token. The token is never logged.

The two services' kinds are split on purpose. ``RemoteFolderUsage`` carries the **blockers**
(live contracts of any lifecycle state, templates, subscriber cards that are not archived, rule
sets that are in use) and the **purges** (contracts already soft-deleted, archived subscriber
cards, rule sets that are not in use). Purging is a second call (:meth:`FolderUsagePort.purge`)
made only after every blocker is zero; it is idempotent.

This module never imports ``dc_contracts`` or ``dc_validation``; it parses the JSON by hand.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog
from dc_auth.verify import unauthenticated
from dc_core.app import LifespanHook
from dc_core.http import make_client
from dc_core.problems import ProblemError
from fastapi import FastAPI

from dc_identity.settings import IdentitySettings

_log = structlog.get_logger("dc_identity.folder_usage")

CONTRACTS_USAGE_PATH = "/internal/v1/contracts/folders/{folder_id}/usage"
VALIDATION_USAGE_PATH = "/internal/v1/validation/folders/{folder_id}/usage"
CONTRACTS_PURGE_PATH = "/internal/v1/contracts/folders/{folder_id}/purge"
VALIDATION_PURGE_PATH = "/internal/v1/validation/folders/{folder_id}/purge"

_CONTRACTS_USAGE_KEYS = (
    "contracts",
    "templates",
    "subscriber_cards",
    "deleted_contracts",
    "archived_subscriber_cards",
)
_VALIDATION_USAGE_KEYS = ("rule_sets", "unused_rule_sets")
_CONTRACTS_PURGE_KEYS = (
    "deleted_contracts",
    "contract_versions",
    "archived_subscriber_cards",
    "card_subscriptions",
    "origin_cleared",
)
_VALIDATION_PURGE_KEYS = ("rule_sets", "rule_set_versions")


@dataclass(frozen=True)
class RemoteFolderUsage:
    """Counts from contracts and validation for one folder.

    Blockers: ``contracts`` (not soft-deleted, any state), ``templates``, ``subscriber_cards``
    (not archived) and ``rule_sets`` (in use). Purges: ``deleted_contracts``,
    ``archived_subscriber_cards`` and ``unused_rule_sets``.
    """

    contracts: int = 0
    templates: int = 0
    subscriber_cards: int = 0
    rule_sets: int = 0
    deleted_contracts: int = 0
    archived_subscriber_cards: int = 0
    unused_rule_sets: int = 0


@dataclass(frozen=True)
class RemotePurge:
    """What the two purge calls removed (for the audit event and the response)."""

    deleted_contracts: int = 0
    contract_versions: int = 0
    archived_subscriber_cards: int = 0
    card_subscriptions: int = 0
    origin_cleared: int = 0
    rule_sets: int = 0
    rule_set_versions: int = 0


class FolderUsagePort(Protocol):
    async def counts(self, folder_id: uuid.UUID, bearer: str) -> RemoteFolderUsage: ...

    async def purge(self, folder_id: uuid.UUID, bearer: str) -> RemotePurge: ...


def unavailable() -> ProblemError:
    """503: the folder contents could not be checked, so nothing is deleted."""
    return ProblemError(
        503,
        "service-unavailable",
        "Service Unavailable",
        "The folder contents could not be checked; nothing was deleted.",
    )


def _counts(body: object, folder_id: uuid.UUID, keys: tuple[str, ...]) -> dict[str, int] | None:
    """The validated counts of one answer, or None when anything is off."""
    if not isinstance(body, dict) or body.get("folder_id") != str(folder_id):
        return None
    counts: dict[str, int] = {}
    for key in keys:
        value = body.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        counts[key] = value
    return counts


class HttpFolderUsage:
    """The two internal HTTP clients (contracts, validation) behind one fail-closed port."""

    def __init__(
        self,
        contracts_http: httpx.AsyncClient,
        validation_http: httpx.AsyncClient,
        *,
        timeout_s: float,
    ) -> None:
        self._contracts = contracts_http
        self._validation = validation_http
        self._timeout_s = timeout_s

    async def _call(
        self,
        service: str,
        http: httpx.AsyncClient,
        method: str,
        path: str,
        folder_id: uuid.UUID,
        bearer: str,
        keys: tuple[str, ...],
    ) -> dict[str, int]:
        try:
            async with asyncio.timeout(self._timeout_s):
                response = await http.request(
                    method,
                    path.format(folder_id=folder_id),
                    headers={"Authorization": f"Bearer {bearer}"},
                    timeout=self._timeout_s,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            _log.warning("folder_usage_unreachable", service=service, error=type(exc).__name__)
            raise unavailable() from exc
        if response.status_code == 401:
            raise unauthenticated()
        if response.status_code == 200:
            try:
                body: Any = response.json()
            except ValueError:
                body = None
            counts = _counts(body, folder_id, keys)
            if counts is not None:
                return counts
        _log.warning("folder_usage_bad_status", service=service, status=response.status_code)
        raise unavailable()

    @staticmethod
    def _first_failure(results: list[Any]) -> None:
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def counts(self, folder_id: uuid.UUID, bearer: str) -> RemoteFolderUsage:
        results = await asyncio.gather(
            self._call(
                "contracts",
                self._contracts,
                "GET",
                CONTRACTS_USAGE_PATH,
                folder_id,
                bearer,
                _CONTRACTS_USAGE_KEYS,
            ),
            self._call(
                "validation",
                self._validation,
                "GET",
                VALIDATION_USAGE_PATH,
                folder_id,
                bearer,
                _VALIDATION_USAGE_KEYS,
            ),
            return_exceptions=True,
        )
        self._first_failure(list(results))
        c, v = results
        assert isinstance(c, dict) and isinstance(v, dict)  # noqa: S101 - narrowed above
        return RemoteFolderUsage(
            contracts=c["contracts"],
            templates=c["templates"],
            subscriber_cards=c["subscriber_cards"],
            rule_sets=v["rule_sets"],
            deleted_contracts=c["deleted_contracts"],
            archived_subscriber_cards=c["archived_subscriber_cards"],
            unused_rule_sets=v["unused_rule_sets"],
        )

    async def purge(self, folder_id: uuid.UUID, bearer: str) -> RemotePurge:
        results = await asyncio.gather(
            self._call(
                "contracts",
                self._contracts,
                "POST",
                CONTRACTS_PURGE_PATH,
                folder_id,
                bearer,
                _CONTRACTS_PURGE_KEYS,
            ),
            self._call(
                "validation",
                self._validation,
                "POST",
                VALIDATION_PURGE_PATH,
                folder_id,
                bearer,
                _VALIDATION_PURGE_KEYS,
            ),
            return_exceptions=True,
        )
        self._first_failure(list(results))
        c, v = results
        assert isinstance(c, dict) and isinstance(v, dict)  # noqa: S101 - narrowed above
        return RemotePurge(**c, **v)


def folder_usage_hook(
    settings: IdentitySettings, *, port: FolderUsagePort | None = None
) -> LifespanHook:
    """Install ``app.state.folder_usage``: the injected port (tests) or the HTTP one.

    The HTTP port is always installed: a wrong ``DC_CONTRACTS_URL`` or ``DC_VALIDATION_URL`` makes
    every delete 503, never a delete on a doubt.
    """

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        if port is not None:
            app.state.folder_usage = port
            yield
            return
        contracts = make_client(
            timeout_s=settings.folder_usage_timeout_s, base_url=settings.contracts_url
        )
        validation = make_client(
            timeout_s=settings.folder_usage_timeout_s, base_url=settings.validation_url
        )
        app.state.folder_usage = HttpFolderUsage(
            contracts, validation, timeout_s=settings.folder_usage_timeout_s
        )
        try:
            yield
        finally:
            await contracts.aclose()
            await validation.aclose()

    return _hook
