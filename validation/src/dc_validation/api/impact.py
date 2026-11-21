"""Rule impact routes (internal): rule-set dry-run and compliance drift (D-158 .. D-161).

``POST /internal/v1/validation/rule-sets/{rule_set_id}/dry-run`` (RULE-10, D-159) evaluates one
rule-set version as if it were enforced against a page of documents. ``ruleset:dry-run`` is
checked at the rule set's folder (``rulesets.rule_set_folder``: an unknown id and a platform set are
the uniform 404); a draft version also needs ``ruleset:read`` there.

``POST /internal/v1/validation/folders/{folder_id}/drift`` (RULE-11, D-160) evaluates each document
against the current mandatory resolution of its own folder (``governance:drift-read`` at the path
folder, which is the scope: every item folder must lie in its subtree).

Both routes are internal: never in OpenAPI and never routed by the gateway. Contracts orchestrates
the pages because it holds the documents (Plan 06-12) and forwards the caller's own bearer token,
so every decision is the caller's. The routes have no Pydantic body parameter: the policy
authenticates and authorises first and the handler reads the capped body afterwards (research
Pitfall 15). Neither route writes anything: no stored run, no audit event, no notice.

Capacity (D-161): a page is capped by ``max_batch_items``, ``max_batch_folders`` and
``max_batch_bytes`` before any PDP call or evaluation (422 with a fixed text), and its evaluation
holds a slot of the separate batch pool (``Sandbox.batch_slot``; saturated is 503 with
``Retry-After``).
"""

from __future__ import annotations

import asyncio
import math
import uuid
from typing import Annotated

from dc_auth.deps import authenticate, current_decision, recorded_refusal, require
from dc_auth.principal import Principal
from dc_core.body import read_body_capped
from dc_core.policy import PolicyRouter
from fastapi import Depends, Request
from pydantic import ValidationError

from dc_validation.api.runs import _shape_problem
from dc_validation.batch import (
    ValidationDriftIn,
    ValidationDriftOut,
    ValidationDryRunIn,
    ValidationDryRunOut,
    decode_items,
    drift,
    dry_run,
)
from dc_validation.rulesets import not_found, rule_set_folder
from dc_validation.settings import ValidationSettings

internal_router = PolicyRouter(prefix="/internal/v1/validation")

# Per document: ids, media type and JSON punctuation around the base64 value.
_ITEM_SLACK = 1024
_ENVELOPE_SLACK = 64 * 1024


def _body_cap(settings: ValidationSettings) -> int:
    """The largest page body read: the byte cap base64 encoded plus the envelope (D-161).

    A larger body is 413 ``payload-too-large``; one under it whose decoded documents exceed
    ``max_batch_bytes`` is the 422 of ``decode_items``.
    """
    return (
        math.ceil(settings.max_batch_bytes * 4 / 3)
        + settings.max_batch_items * _ITEM_SLACK
        + _ENVELOPE_SLACK
    )


@internal_router.post(
    "/rule-sets/{rule_set_id}/dry-run",
    response_model=ValidationDryRunOut,
    dependencies=[require("ruleset:dry-run", target=rule_set_folder)],
)
async def rule_set_dry_run(
    rule_set_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> ValidationDryRunOut:
    """Evaluate one version of the rule set, as if enforced, against a page of documents."""
    decision = current_decision(request)
    if decision.folder is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise not_found()
    settings: ValidationSettings = request.app.state.settings
    raw = await read_body_capped(request, _body_cap(settings))
    try:
        body = await asyncio.to_thread(ValidationDryRunIn.model_validate_json, raw)
    except ValidationError as exc:
        raise _shape_problem(exc) from exc
    if body.version == "draft" and "ruleset:read" not in decision.permissions:
        # Drafts are read with ruleset:read only; ruleset:read is its own visibility (D-08).
        # The PDP is asked for ruleset:read first, so the refusal is a recorded denial (WR-01).
        raise await recorded_refusal(request, "ruleset:read", decision.folder.id)
    items = decode_items(body.items, settings)
    authorizer = request.app.state.authorizer_factory(request)
    return await dry_run(
        request.app.state,
        principal,
        authorizer,
        rule_set_id=rule_set_id,
        rule_set_folder_id=decision.folder.id,
        version=body.version,
        scope_folder_id=body.scope_folder_id,
        items=items,
        disconnected=request.is_disconnected,
    )


async def _path_folder_id(folder_id: uuid.UUID) -> uuid.UUID:
    return folder_id


@internal_router.post(
    "/folders/{folder_id}/drift",
    response_model=ValidationDriftOut,
    dependencies=[require("governance:drift-read", target=_path_folder_id)],
)
async def folder_drift(
    folder_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> ValidationDriftOut:
    """Classify a page of documents as drifted, at risk or compliant at their own folders."""
    settings: ValidationSettings = request.app.state.settings
    raw = await read_body_capped(request, _body_cap(settings))
    try:
        body = await asyncio.to_thread(ValidationDriftIn.model_validate_json, raw)
    except ValidationError as exc:
        raise _shape_problem(exc) from exc
    items = decode_items(body.items, settings)
    authorizer = request.app.state.authorizer_factory(request)
    return await drift(
        request.app.state,
        principal,
        authorizer,
        folder_id=folder_id,
        items=items,
        disconnected=request.is_disconnected,
    )
