"""Governance routes: rule-set dry-runs and compliance drift (RULE-10, RULE-11, D-158 .. D-161).

* ``POST /api/v1/contracts/governance/rule-set-dry-runs`` with the JSON body
  ``{rule_set_id, version: n | "draft", folder_id?, cursor?}`` (D-159). ``ruleset:dry-run`` is
  checked at the rule set's folder, which contracts learns from validation with the caller's own
  token (``ValidationClient.rule_set_folder``): an unknown id, a rule set the caller cannot read and
  a platform set are the uniform 404. There is no FastAPI body parameter: FastAPI would buffer an
  uncapped body before the policy runs. The target resolver runs after authentication, reads the
  small body with ``read_body_capped`` (``DC_IMPACT_REQUEST_MAX_BYTES``, 413 above it), parses it
  strictly (422) and leaves the parsed model on ``request.state``. ``folder_id`` only narrows the
  candidates; it never moves the permission check.
* ``GET /api/v1/contracts/governance/drift?folder_id=&cursor=`` (D-160). ``governance:drift-read``
  is checked at the query folder (``_query_folder_id``); a missing or malformed ``folder_id`` is
  422 before any authorisation call. Drifted and at-risk contracts are listed, compliant ones
  counted. Purely informational: no state change and no notice.

Both routes change no state and notify nobody (D-161), but every answered call is audited
(CR-01): ``governance.rule_set_dry_run`` on the rule set and ``governance.drift_read`` on the
folder, each with the actor, the scope folder and the page's counts only (never a contract id,
name, rule key or anything from a document). The audit row is the only write; it commits after the
page was evaluated, so no transaction is open while validation runs. The work is in
``dc_contracts.impact``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from dc_auth.deps import (
    UnitOfWorkDep,
    authenticate,
    current_decision,
    parse_bearer,
    require,
)
from dc_auth.principal import Principal
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dc_contracts.impact import (
    ContractsDriftOut,
    ContractsRuleSetDryRunOut,
    DryRunRequest,
    drift_page,
    dry_run_page,
    parse_cursor,
)
from dc_contracts.ingest import read_body_capped
from dc_contracts.settings import ContractsSettings
from dc_contracts.validation_client import ValidationClient, unavailable

router = PolicyRouter(prefix="/api/v1/contracts")

# A published version number (validation's column is a 32-bit integer) or the draft.
_VersionNo = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]


class ContractsRuleSetDryRunIn(BaseModel):
    """The dry-run request: which version, an optional narrowing folder, the cursor."""

    model_config = ConfigDict(extra="forbid")

    rule_set_id: uuid.UUID
    version: _VersionNo | Literal["draft"]
    folder_id: uuid.UUID | None = None
    cursor: str | None = Field(default=None, max_length=200)


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


def _pointer(loc: tuple[object, ...]) -> str:
    parts = (str(p).replace("~", "~0").replace("/", "~1") for p in loc)
    return "/" + "/".join(("body", *parts))


def _invalid_body(exc: ValidationError) -> ProblemError:
    """422 with JSON Pointers and fixed texts: nothing submitted is echoed back."""
    return ProblemError(
        422,
        "validation-error",
        "Unprocessable Content",
        "The request failed validation.",
        errors=[
            {
                "pointer": _pointer(tuple(err.get("loc", ()))),
                "detail": f"Invalid value ({err.get('type', 'invalid')}).",
            }
            for err in exc.errors()
        ],
    )


async def _dry_run_target(
    request: Request, principal: Annotated[Principal, Depends(authenticate)]
) -> uuid.UUID:
    """Target resolver: the rule set's folder, after the capped body was read and parsed.

    ``principal`` makes this run after authentication, so an anonymous caller never makes the
    service read anything. No contract is queried here.
    """
    settings: ContractsSettings = request.app.state.settings
    raw = await read_body_capped(request, settings.impact_request_max_bytes)
    try:
        body = ContractsRuleSetDryRunIn.model_validate_json(raw)
    except ValidationError as exc:
        raise _invalid_body(exc) from None
    request.state.impact_dry_run = body
    client: ValidationClient | None = getattr(request.app.state, "validation_client", None)
    if client is None:
        raise unavailable("Contract validation is not configured.")
    folder_id = await client.rule_set_folder(body.rule_set_id, parse_bearer(request))
    if folder_id is None:
        raise _not_found()
    return folder_id


@router.post(
    "/governance/rule-set-dry-runs",
    response_model=ContractsRuleSetDryRunOut,
    dependencies=[require("ruleset:dry-run", target=_dry_run_target)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": ContractsRuleSetDryRunIn.model_json_schema()}
            },
        }
    },
    responses={
        401: {"description": "No valid token"},
        404: {
            "description": "Rule set or version not found, a platform rule set, or not visible "
            "to the caller (one response for every reason)"
        },
        413: {"description": "Request body larger than DC_IMPACT_REQUEST_MAX_BYTES"},
        422: {"description": "Invalid body or cursor"},
        503: {"description": "Validation or authorization is temporarily unavailable"},
    },
)
async def rule_set_dry_run(
    request: Request, principal: Annotated[Principal, Depends(authenticate)], uow: UnitOfWorkDep
) -> ContractsRuleSetDryRunOut:
    """Evaluate one rule-set version, as if enforced, against the next page of contracts."""
    body: ContractsRuleSetDryRunIn = request.state.impact_dry_run
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    after = parse_cursor(body.cursor, "/body/cursor")
    state = request.app.state
    page = await dry_run_page(
        session_factory=state.session_factory,
        settings=state.settings,
        client=state.validation_client,
        authorizer=state.authorizer_factory(request),
        principal=principal,
        bearer=parse_bearer(request),
        req=DryRunRequest(
            rule_set_id=body.rule_set_id,
            version=body.version,
            rule_set_folder_id=facts.id,
            rule_set_ancestor_ids=facts.ancestor_ids,
            folder_id=body.folder_id,
            cursor=after,
        ),
    )
    uow.audit(
        "governance.rule_set_dry_run",
        "rule_set",
        body.rule_set_id,
        before=None,
        after={
            "version": page.version,
            "scope_folder_id": str(page.scope_folder_id),
            "continued": after is not None,
            "listed": len(page.items),
            "total_in_scope": page.total_in_scope,
            "would_fail": page.summary.would_fail,
            "would_error": page.summary.would_error,
            "pass": page.summary.pass_,
            "hidden": page.summary.hidden,
        },
        scope_path=facts.ancestor_ids,
    )
    await uow.commit()
    return page


async def _query_folder_id(folder_id: Annotated[uuid.UUID, Query()]) -> uuid.UUID:
    """Target resolver: the scope folder named in the query (FastAPI validates it first, 422)."""
    return folder_id


@router.get(
    "/governance/drift",
    response_model=ContractsDriftOut,
    dependencies=[require("governance:drift-read", target=_query_folder_id)],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "The caller can read the folder but may not read compliance drift"},
        404: {"description": "Folder not found, or not visible to the caller"},
        422: {"description": "Missing or invalid folder_id, or an invalid cursor"},
        503: {"description": "Validation or authorization is temporarily unavailable"},
    },
)
async def compliance_drift(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
    folder_id: Annotated[uuid.UUID, Query()],
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> ContractsDriftOut:
    """List the published contracts under the folder that no longer pass the rules in force."""
    after = parse_cursor(cursor, "/query/cursor")
    facts = current_decision(request).folder
    state = request.app.state
    page = await drift_page(
        session_factory=state.session_factory,
        settings=state.settings,
        client=state.validation_client,
        authorizer=state.authorizer_factory(request),
        principal=principal,
        bearer=parse_bearer(request),
        folder_id=folder_id,
        cursor=after,
    )
    uow.audit(
        "governance.drift_read",
        "folder",
        folder_id,
        before=None,
        after={
            "continued": after is not None,
            "listed": len(page.items),
            "total_in_scope": page.total_in_scope,
            **page.summary.model_dump(),
        },
        scope_path=facts.ancestor_ids if facts is not None else (folder_id,),
    )
    await uow.commit()
    return page
