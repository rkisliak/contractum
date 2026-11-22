"""The publish gate: contracts asks validation whether the stored bytes may become a version.

``ValidationPublishGate`` is the real gate (VAL-07, D-126, D-131). It first runs the D-19 ODCS
acceptance check locally (unchanged errors), then posts the raw bytes to validation's internal
``POST /internal/v1/validation/folders/{folder_id}/runs`` in publish mode with the **caller's own**
bearer token, so validation authorises ``contract:validate`` itself (no confused deputy, T-5-40).
Everything that is not a clean answer is a refusal: an unreachable, slow, saturated or failing
validation service is 503 ``validation-unavailable`` and never a pass (T-5-37); a different content
hash than the one gated is 503 too (T-5-38); a report that is not publishable is 422
``contract-validation-failed`` carrying the report and the run id.

``FailClosedGate`` is what ``get_publish_gate`` falls back to when no gate was installed: it
refuses every publish, so a missing configuration can never become a pass-through.

The token is never logged. contracts never imports ``dc_validation`` or ``dc_rules`` (D-135): the
report travels as JSON and is documented in ``docs/user/api-guide.md``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import structlog
from dc_auth.authorizer import FolderFacts
from dc_auth.authz_wire import passthrough_problem
from dc_auth.deps import current_decision, parse_bearer
from dc_auth.verify import unauthenticated
from dc_core.app import LifespanHook
from dc_core.http import make_client
from dc_core.problems import ProblemError
from dc_odcs import ParsedContract
from fastapi import FastAPI, Request

from dc_contracts.compat import BaselineFacts, baseline_and_floor, baseline_payload
from dc_contracts.drafts import get_draft
from dc_contracts.ingest import parse_upload
from dc_contracts.link_facts import LinkFactsResolver, draft_link_resolver
from dc_contracts.settings import ContractsSettings

_log = structlog.get_logger("dc_contracts.validation_client")

RUNS_PATH = "/internal/v1/validation/folders/{folder_id}/runs"
RUN_READ_PATH = "/internal/v1/validation/runs/{run_id}"
AVAILABLE_RULE_SET_PATH = "/api/v1/validation/folders/{folder_id}/available-rule-sets/{rule_set_id}"
RULE_SET_PATH = "/api/v1/validation/rule-sets/{rule_set_id}"
DRY_RUN_PATH = "/internal/v1/validation/rule-sets/{rule_set_id}/dry-run"
DRIFT_PATH = "/internal/v1/validation/folders/{folder_id}/drift"

# The only text an id may put into a validation URL path: the canonical lowercase form of a UUID.
_PATH_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _path_id(value: uuid.UUID) -> str:
    """``value`` as a URL path segment, refused unless it is a canonical UUID.

    Every id that reaches a validation path is a ``uuid.UUID`` already; this check makes that a
    property of the URL builder itself, so no caller can ever put ``/``, ``?``, ``..`` or any other
    path text into the request target.
    """
    text = str(value)
    if not _PATH_ID.fullmatch(text):
        raise ValueError("a validation path id must be a canonical UUID")
    return text


# Keys of a validation pin that become part of the published version (D-132). The wire pin also
# carries a ``deprecated_version`` hint, which is not part of what a version was validated against.
PIN_KEYS = (
    "rule_set_id",
    "name",
    "folder_id",
    "scope",
    "version",
    "source",
    "mode",
    "dsl_version",
)


def gate_pin(pin: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a wire pin that is stored with a published version (D-132).

    ``paused: true`` is added only for a pin of a paused platform rule set (quick 261118-vpa), so
    the stored history of every unpaused pin is byte-identical to before the pause existed. The
    marker is built from validation's own answer; a client cannot supply pins.
    """
    projected = {key: pin.get(key) for key in PIN_KEYS}
    if pin.get("paused") is True:
        projected["paused"] = True
    return projected


@dataclass(frozen=True)
class GateContext:
    """What the gate needs besides the bytes: who is publishing what, where (D-131)."""

    contract_id: uuid.UUID
    draft_id: uuid.UUID
    facts: FolderFacts
    bearer: str = field(repr=False)
    attachments: tuple[dict[str, Any], ...] = ()
    attachments_revision: int | None = None
    # D-146, D-147: the newest published version and the highest stable major, read unlocked just
    # before the call and re-resolved under the contract lock. None on a first publish.
    baseline: BaselineFacts | None = None
    stable_major_floor: int | None = None
    # D-153: caller-scoped facts about the document's contract links (see ``link_facts``).
    ref_facts: dict[str, dict[str, Any]] | None = None
    # Reads ``ref_facts`` for the draft in the caller's unlocked read session (never a lock).
    link_resolver: LinkFactsResolver | None = field(default=None, repr=False)


@dataclass(frozen=True)
class GateResult:
    """The gate's verdict for bytes it let through (never produced for a refusal)."""

    parsed: ParsedContract
    run_id: uuid.UUID | None
    pins: list[dict[str, Any]]
    engine_version: str | None
    schema_sha: str | None
    attachments_revision: int | None
    # What the verdict was computed against; publish re-resolves both under the lock (D-147).
    baseline_id: uuid.UUID | None = None
    stable_major_floor: int | None = None


def unavailable(detail: str, *, retry_after: str | None = None) -> ProblemError:
    """503 ``validation-unavailable``: the gate could not decide, so nothing is published."""
    return ProblemError(
        503,
        "validation-unavailable",
        "Service Unavailable",
        detail,
        headers={"Retry-After": retry_after} if retry_after else None,
    )


def _problem_type(content: bytes) -> str | None:
    """The ``type`` of an RFC 9457 body when it is one of validation's urns, else ``None``."""
    try:
        body = json.loads(content)
    except ValueError:
        return None
    value = body.get("type") if isinstance(body, dict) else None
    return value[:100] if isinstance(value, str) and value.startswith("urn:dc:problem:") else None


class FailClosedGate:
    """The fallback when no gate is installed: every publish is refused (research Pitfall 2)."""

    async def check(
        self, raw: bytes, media_type: str, settings: ContractsSettings, ctx: GateContext
    ) -> GateResult:
        raise unavailable("Contract validation is not configured; the contract was not published.")


def _retry_after(response: httpx.Response) -> str | None:
    value = response.headers.get("retry-after")
    return value if value and value.strip().isdecimal() else None


class ValidationClient:
    """One call to validation's internal run route, with its answer mapped to problems."""

    def __init__(self, http: httpx.AsyncClient, settings: ContractsSettings) -> None:
        self._http = http
        self._settings = settings

    async def run(
        self,
        ctx: GateContext,
        raw: bytes,
        media_type: str,
        mode: Literal["save", "publish"],
    ) -> dict[str, Any]:
        """The run's JSON answer (``ValidationRunOut``) or a ``ProblemError``."""
        timeout = (
            self._settings.validation_publish_timeout_s
            if mode == "publish"
            else self._settings.validation_save_timeout_s
        )
        payload = {
            "mode": mode,
            "contract_id": str(ctx.contract_id),
            "draft_id": str(ctx.draft_id),
            "media_type": media_type,
            "document_b64": base64.b64encode(raw).decode("ascii"),
            "attachments": list(ctx.attachments),
            "attachments_revision": ctx.attachments_revision,
            "baseline": baseline_payload(ctx.baseline),
            "stable_major_floor": ctx.stable_major_floor,
        }
        if ctx.ref_facts is not None:
            payload["ref_facts"] = {"contracts": ctx.ref_facts}
        try:
            # httpx timeouts bound each socket operation, not the whole call: the deadline below
            # bounds the call itself, so a validation service that answers slowly is a refusal.
            async with asyncio.timeout(timeout):
                response = await self._http.post(
                    RUNS_PATH.format(folder_id=_path_id(ctx.facts.id)),
                    content=json.dumps(payload),
                    headers={
                        "Authorization": f"Bearer {ctx.bearer}",
                        "Content-Type": "application/json",
                    },
                    timeout=timeout,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            _log.warning("validation_unreachable", error=type(exc).__name__)
            raise unavailable("Contract validation is temporarily unavailable.") from exc
        return self._answer(response)

    async def available_rule_set(
        self, bearer: str, folder_id: uuid.UUID, rule_set_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """The published rule set as visible at ``folder_id`` for the caller, or None (404).

        Validation decides visibility with the caller's own bearer (D-123, T-5-44). Anything but a
        clean 200 or 404 (transport, timeout, 5xx, 403, an unreadable body) is 503
        ``validation-unavailable``: nothing is attached on a doubt.
        """
        timeout = self._settings.validation_save_timeout_s
        try:
            async with asyncio.timeout(timeout):
                response = await self._http.get(
                    AVAILABLE_RULE_SET_PATH.format(
                        folder_id=_path_id(folder_id), rule_set_id=_path_id(rule_set_id)
                    ),
                    headers={"Authorization": f"Bearer {bearer}"},
                    timeout=timeout,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            _log.warning("validation_unreachable", error=type(exc).__name__)
            raise unavailable("Contract validation is temporarily unavailable.") from exc
        if response.status_code == 404:
            return None
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                return body
        _log.warning("validation_bad_status", status=response.status_code)
        raise unavailable(
            "Contract validation is temporarily unavailable.",
            retry_after=_retry_after(response),
        )

    async def get_run(
        self, run_id: uuid.UUID, contract_id: uuid.UUID, bearer: str
    ) -> dict[str, Any] | None:
        """The stored publish run (``ValidationRunReadOut``) for the report fetch, None for a 404.

        Validation authorises the read itself with the caller's own bearer (D-155, T-6-26): the
        run's stored folder and contract must match, so a 404 means "not yours to read". Anything
        but a clean 200 or 404 (transport, timeout, 5xx, 403, an unreadable body) is 503
        ``validation-unavailable``: a report is never answered empty on a doubt.
        """
        timeout = self._settings.validation_save_timeout_s
        try:
            async with asyncio.timeout(timeout):
                response = await self._http.get(
                    RUN_READ_PATH.format(run_id=_path_id(run_id)),
                    params={"contract_id": str(contract_id)},
                    headers={"Authorization": f"Bearer {bearer}"},
                    timeout=timeout,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            _log.warning("validation_unreachable", error=type(exc).__name__)
            raise unavailable("Contract validation is temporarily unavailable.") from exc
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            raise unauthenticated()
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and isinstance(body.get("report"), dict):
                return body
        _log.warning("validation_bad_status", status=response.status_code)
        raise unavailable(
            "Contract validation is temporarily unavailable.",
            retry_after=_retry_after(response),
        )

    async def rule_set_folder(self, rule_set_id: uuid.UUID, bearer: str) -> uuid.UUID | None:
        """The folder holding the rule set as the caller sees it, or None (RULE-10, D-159).

        Validation answers its own ``GET /rule-sets/{rule_set_id}`` with the caller's bearer, so a
        rule set the caller cannot read is a 404 there. A 404 or a 403 (the folder is visible but
        the drafts are not, so ``ruleset:dry-run`` would be the uniform 404 anyway), and a platform
        set (``rule_set.folder_id`` null), are all None. Anything else but a clean 200 is 503
        ``validation-unavailable``: no contract is listed on a doubt.
        """
        timeout = self._settings.validation_save_timeout_s
        try:
            async with asyncio.timeout(timeout):
                response = await self._http.get(
                    RULE_SET_PATH.format(rule_set_id=_path_id(rule_set_id)),
                    headers={"Authorization": f"Bearer {bearer}"},
                    timeout=timeout,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            _log.warning("validation_unreachable", error=type(exc).__name__)
            raise unavailable("Contract validation is temporarily unavailable.") from exc
        if response.status_code in (403, 404):
            return None
        if response.status_code == 401:
            raise unauthenticated()
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                body = None
            rule_set = body.get("rule_set") if isinstance(body, dict) else None
            if isinstance(rule_set, dict) and "folder_id" in rule_set:
                folder = rule_set["folder_id"]
                if folder is None:
                    return None
                try:
                    return uuid.UUID(str(folder))
                except ValueError:
                    pass
        _log.warning("validation_bad_status", status=response.status_code)
        raise unavailable(
            "Contract validation is temporarily unavailable.",
            retry_after=_retry_after(response),
        )

    async def dry_run(
        self,
        rule_set_id: uuid.UUID,
        version: int | Literal["draft"],
        scope_folder_id: uuid.UUID,
        items: list[dict[str, Any]],
        bearer: str,
    ) -> dict[str, Any]:
        """One dry-run page (``ValidationDryRunOut``) for documents contracts selected (D-158)."""
        payload = {"version": version, "scope_folder_id": str(scope_folder_id), "items": items}
        return await self._batch(
            DRY_RUN_PATH.format(rule_set_id=_path_id(rule_set_id)), payload, bearer
        )

    async def drift(
        self, folder_id: uuid.UUID, items: list[dict[str, Any]], bearer: str
    ) -> dict[str, Any]:
        """One compliance-drift page (``ValidationDriftOut``) at ``folder_id`` (D-160)."""
        return await self._batch(
            DRIFT_PATH.format(folder_id=_path_id(folder_id)), {"items": items}, bearer
        )

    async def _batch(self, path: str, payload: dict[str, Any], bearer: str) -> dict[str, Any]:
        """POST a batch page with the caller's bearer; a clean 200 body or a ``ProblemError``.

        401 is the caller's own 401; a known identity 403 passes through; 404 is the uniform 404
        (the version or a folder is not the caller's to evaluate); a saturated or failing
        validation service is 503 with its ``Retry-After``; any other 4xx is a limit mismatch
        between the two services and is 503 too (D-161). Nothing is retried here.
        """
        timeout = self._settings.validation_impact_timeout_s
        try:
            async with asyncio.timeout(timeout):
                response = await self._http.post(
                    path,
                    content=json.dumps(payload),
                    headers={
                        "Authorization": f"Bearer {bearer}",
                        "Content-Type": "application/json",
                    },
                    timeout=timeout,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            _log.warning("validation_unreachable", error=type(exc).__name__)
            raise unavailable("Contract validation is temporarily unavailable.") from exc
        status = response.status_code
        if status == 200:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                return body
            _log.warning("validation_bad_answer")
            raise unavailable("Contract validation answered with an unreadable response.")
        if status == 401:
            raise unauthenticated()
        problem = passthrough_problem(status, response.content)
        if problem is not None:
            raise problem
        if status == 404:
            raise ProblemError(404, "not-found", "Not Found", "The resource was not found.")
        if status >= 500 or status in (408, 429):
            _log.warning("validation_bad_status", status=status)
            raise unavailable(
                "Contract validation is temporarily unavailable.",
                retry_after=_retry_after(response),
            )
        _log.warning(
            "validation_refused", status=status, problem_type=_problem_type(response.content)
        )
        raise unavailable("Contract validation could not process the request.")

    @staticmethod
    def _answer(response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if status == 200:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and isinstance(body.get("report"), dict):
                return body
            _log.warning("validation_bad_answer")
            raise unavailable("Contract validation answered with an unreadable response.")
        if status == 401:
            raise unauthenticated()
        problem = passthrough_problem(status, response.content)
        if problem is not None:
            raise problem
        if status >= 500 or status in (408, 429):
            _log.warning("validation_bad_status", status=status)
            raise unavailable(
                "Contract validation is temporarily unavailable.",
                retry_after=_retry_after(response),
            )
        if status == 403:
            _log.warning("validation_refused", status=status)
            raise ProblemError(
                403, "forbidden", "Forbidden", "You cannot validate contracts in this folder."
            )
        # WR-07: any other 4xx (400, 404, 413, 415, 422) on the internal run route is not a
        # permission problem: contracts' own policy already authorised the caller. It is a limit
        # mismatch or a bug between the two services, so nothing publishes (503) and the log names
        # the status and validation's problem type (a fixed urn, never a document value).
        _log.warning(
            "validation_refused", status=status, problem_type=_problem_type(response.content)
        )
        raise unavailable("Contract validation could not process the request.")


class ValidationPublishGate:
    """The real gate: local acceptance check, then a publish-mode validation run."""

    def __init__(self, client: ValidationClient) -> None:
        self._client = client

    async def check(
        self, raw: bytes, media_type: str, settings: ContractsSettings, ctx: GateContext
    ) -> GateResult:
        parsed = await parse_upload(raw, media_type, settings)
        answer = await self._client.run(ctx, raw, media_type, "publish")
        if answer.get("content_sha256") != parsed.content_sha256:
            _log.warning("validation_hash_mismatch")
            raise unavailable(
                "Contract validation checked different bytes than the draft; retry the publish."
            )
        report: dict[str, Any] = answer["report"]
        summary = report.get("summary")
        publishable = summary.get("publishable") if isinstance(summary, dict) else None
        run_id = answer.get("run_id")
        if publishable is not True:
            blocking = summary.get("blocking_count", 0) if isinstance(summary, dict) else 0
            raise ProblemError(
                422,
                "contract-validation-failed",
                "Unprocessable Content",
                f"The contract fails {blocking} blocking rule(s) or its ODCS schema; see report.",
                report=report,
                run_id=run_id,
            )
        pins = [gate_pin(pin) for pin in answer.get("pins", [])]
        return GateResult(
            parsed=parsed,
            run_id=uuid.UUID(run_id) if run_id else None,
            pins=pins,
            engine_version=answer.get("engine_version"),
            schema_sha=answer.get("schema_sha"),
            attachments_revision=answer.get("attachments_revision"),
            baseline_id=ctx.baseline.version_id if ctx.baseline is not None else None,
            stable_major_floor=ctx.stable_major_floor,
        )


async def _save_mode_run(
    request: Request, contract_id: uuid.UUID, draft_id: uuid.UUID, raw: bytes, media_type: str
) -> dict[str, Any]:
    """One save-mode call with the contract's current attachments; the run's JSON answer.

    The attachments are read in a fresh short session that is closed before the network call, so
    no lock or transaction is held while validation works (T-5-50). Raises ``ProblemError`` on
    anything that is not a clean answer.
    """
    # Imported here: attachments imports this module for ValidationClient.
    from dc_contracts.attachments import attachments_for_gate

    facts = current_decision(request).folder
    client: ValidationClient | None = getattr(request.app.state, "validation_client", None)
    if facts is None or client is None:
        raise unavailable("Contract validation is not configured.")
    async with request.app.state.session_factory() as session:
        attachments, revision = await attachments_for_gate(session, contract_id)
        # D-148: the same baseline and floor a publish would use, so the saved draft's report
        # already says which version the publish needs.
        baseline = await baseline_and_floor(session, contract_id)
        # D-153: link facts scoped to the caller, read in the same short session.
        link_facts = await draft_link_resolver(
            request.app.state.authorizer_factory(request),
            request.state.principal,
            contract_id,
            draft_id,
        )(session)
    ctx = GateContext(
        contract_id=contract_id,
        draft_id=draft_id,
        facts=facts,
        bearer=parse_bearer(request),
        attachments=attachments,
        attachments_revision=revision,
        baseline=baseline,
        stable_major_floor=baseline.floor if baseline is not None else None,
        ref_facts=link_facts.facts,
    )
    return await client.run(ctx, raw, media_type, "save")


async def save_mode_embed(
    request: Request, contract_id: uuid.UUID, draft_id: uuid.UUID, raw: bytes, media_type: str
) -> dict[str, Any]:
    """The advisory save-mode report for a draft that was just saved (VAL-06, D-134).

    Never raises: the report dict (``status`` ``completed``) on a clean answer, otherwise
    ``{"status": "unavailable"}``, because a save must not depend on a remote service (T-5-49).
    Call it after the commit. Nothing is stored and nothing is cached.
    """
    try:
        answer = await _save_mode_run(request, contract_id, draft_id, raw, media_type)
        report: dict[str, Any] = answer["report"]
    except Exception as exc:
        _log.warning("save_validation_unavailable", error=type(exc).__name__)
        return {"status": "unavailable"}
    return report


async def validate_draft(
    request: Request, contract_id: uuid.UUID, draft_id: uuid.UUID
) -> dict[str, Any]:
    """Validate a stored draft on demand (VAL-08, D-126): the run's answer; 404 for no such draft.

    The caller already passed ``contract:read``. A validation outage is 503
    ``validation-unavailable`` because the report was asked for explicitly. Nothing is stored and
    the draft is not changed.
    """
    async with request.app.state.session_factory() as session:
        draft = await get_draft(session, contract_id, draft_id)
        if draft is None:
            raise ProblemError(404, "not-found", "Not Found", "The resource was not found.")
        raw, media_type = bytes(draft.raw_document), draft.media_type
    return await _save_mode_run(request, contract_id, draft_id, raw, media_type)


def validation_hook(
    settings: ContractsSettings, *, http_client: httpx.AsyncClient | None = None
) -> LifespanHook:
    """Install the real gate on ``app.state.publish_gate`` for the app's lifetime.

    The gate is installed unconditionally: a missing or wrong ``DC_VALIDATION_URL`` makes every
    publish 503, never a pass (D-131). An injected client (tests) is not closed here.
    """

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        http = http_client or make_client(
            timeout_s=settings.validation_publish_timeout_s, base_url=settings.validation_url
        )
        client = ValidationClient(http, settings)
        app.state.validation_client = client
        app.state.publish_gate = ValidationPublishGate(client)
        try:
            yield
        finally:
            if http_client is None:
                await http.aclose()

    return _hook
