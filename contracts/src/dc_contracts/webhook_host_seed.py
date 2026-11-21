"""One-time seed of the webhook allow-list from the channels that already exist (quick 261120-s0p).

The allow-list of webhook hosts is mandatory: an empty list allows no host. A deployment that
configured webhook channels before that rule would stop delivering on upgrade, so at start-up
contracts hands the distinct hosts of every non-archived card with a ``webhook_url`` (enabled or
not) to identity, which seeds its list exactly once (a marker row) and never widens a list an
admin already stored. Identity owns the settings and contracts owns the channels, and neither may
read the other's database, so this is one internal call with the notifier key.

Until the seed is confirmed in this process, ``WebhookHostSeed.done`` is False: due webhook
deliveries are deferred (``settings-unavailable``, no attempt used) and a webhook URL edit is
503, so nothing is lost or wrongly refused during a rolling upgrade. Without a notifier key
(``mail_api_key``) or with ``DC_WEBHOOK_HOST_SEED_ENABLED`` false the seed is done at once (that
deployment cannot read the list and allows no webhook host). Every failure is retried with
exponential backoff capped at 300 seconds; logs carry counts, a status or an error class only,
never a host.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import structlog
from dc_core.app import LifespanHook
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.models import SubscriberCard
from dc_contracts.settings import ContractsSettings

_log = structlog.get_logger("dc_contracts.webhook_host_seed")

SEED_PATH = "/internal/v1/settings/webhook-hosts/seed"
RETRY_INITIAL_S = 1.0
RETRY_MAX_S = 300.0
# Identity's route takes at most this many hosts, each at most 253 characters.
MAX_HOSTS = 1000
MAX_HOST_CHARS = 253
CALL_TIMEOUT_S = 10.0


class WebhookHostSeed:
    """Whether this process knows the webhook allow-list was seeded (or needs no seed)."""

    def __init__(self, done: bool = False) -> None:
        self.done = done


def normalise_host(url: str) -> str | None:
    """The host of ``url`` as the allow-list compares it: IDNA ASCII, lowercase, no trailing dot."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    try:
        name = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if not name or len(name) > MAX_HOST_CHARS:
        return None
    return name


async def configured_hosts(session: AsyncSession) -> list[str]:
    """Distinct normalised hosts of every non-archived card with a webhook URL, sorted."""
    urls = (
        (
            await session.execute(
                select(SubscriberCard.webhook_url)
                .where(
                    SubscriberCard.webhook_url.is_not(None),
                    SubscriberCard.archived_at.is_(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    hosts = {host for url in urls if url and (host := normalise_host(url)) is not None}
    return sorted(hosts)


class _Refused(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


async def _seed_once(app: FastAPI, key: SecretStr) -> dict[str, int]:
    async with app.state.session_factory() as session:
        hosts = await configured_hosts(session)
    base = app.state.auth_settings.identity_internal_url.rstrip("/")
    response = await app.state.http.post(
        f"{base}{SEED_PATH}",
        json={"hosts": hosts[:MAX_HOSTS]},
        headers={"Authorization": f"Bearer {key.get_secret_value()}"},
        timeout=CALL_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise _Refused(response.status_code)
    body = response.json()
    return {
        "configured": len(hosts),
        "added": int(body["added"]),
        "skipped": int(body["skipped"]),
        "seeded": int(bool(body["seeded"])),
    }


async def run_seed(
    app: FastAPI,
    settings: ContractsSettings,
    seed: WebhookHostSeed,
    *,
    retry_initial_s: float = RETRY_INITIAL_S,
    retry_max_s: float = RETRY_MAX_S,
) -> None:
    """Post the hosts until identity answers, then mark ``seed`` done."""
    key = settings.mail_api_key
    if key is None:
        raise ValueError("the webhook host seed needs the notifier key")
    delay = retry_initial_s
    while True:
        try:
            counts = await _seed_once(app, key)
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = exc.status if isinstance(exc, _Refused) else None
            _log.warning(
                "webhook_host_seed_retry",
                status=status,
                error=None if status is not None else type(exc).__name__,
                wait_s=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, retry_max_s)
    # Order matters: drop the cached (pre-seed) list first, so a pass that sees ``done`` reads
    # the seeded one.
    client = getattr(app.state, "settings_client", None)
    if client is not None:
        client.invalidate()
    seed.done = True
    _log.info("webhook_host_seed_done", **counts)


def webhook_host_seed_hook(
    settings: ContractsSettings,
    *,
    retry_initial_s: float = RETRY_INITIAL_S,
    retry_max_s: float = RETRY_MAX_S,
) -> LifespanHook:
    """Lifespan hook: ``app.state.webhook_host_seed`` and the background seed task.

    Must run after ``auth_lifespan`` (``app.state.http``, ``auth_settings``) and
    ``settings_client_hook``, and before ``notifier_hook`` (which reads the seed object).
    """

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        seed = WebhookHostSeed()
        app.state.webhook_host_seed = seed
        task: asyncio.Task[None] | None = None
        if settings.mail_api_key is None or not settings.webhook_host_seed_enabled:
            seed.done = True
            reason = "disabled" if not settings.webhook_host_seed_enabled else "no-notifier-key"
            _log.info("webhook_host_seed_skipped", reason=reason)
        else:
            task = asyncio.create_task(
                run_seed(
                    app,
                    settings,
                    seed,
                    retry_initial_s=retry_initial_s,
                    retry_max_s=retry_max_s,
                ),
                name="webhook-host-seed",
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    return _hook


__all__ = [
    "WebhookHostSeed",
    "configured_hosts",
    "normalise_host",
    "run_seed",
    "webhook_host_seed_hook",
]
