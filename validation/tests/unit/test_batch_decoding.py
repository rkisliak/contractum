"""Rule-impact pages: decoding, folder facts and per-document verdicts without a database (D-161).

The routes and the PDP are covered end to end in ``tests/integration/test_batch_evaluate.py``;
these are the fail-closed edges of the helpers they are built from.
"""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from dc_auth.authorizer import AuthorizerUnavailable
from dc_auth.principal import Principal
from dc_core.problems import ProblemError
from dc_rules.model import RuleDef
from dc_rules.protocol import UnitResult
from dc_validation.batch import (
    BatchItem,
    ValidationBatchItemIn,
    _evaluate_item,
    _load_dry_run_version,
    _Prepared,
    _verdicts,
    decode_items,
    folder_facts,
)
from dc_validation.report import RuleBinding
from dc_validation.resolution import ResolvedRuleSet
from dc_validation.settings import ValidationSettings

pytestmark = pytest.mark.unit

FOLDER = uuid.UUID("00000000-0000-7000-8000-0000000000f1")
CONTRACT = uuid.UUID("00000000-0000-7000-8000-0000000000c1")


def settings() -> ValidationSettings:
    return ValidationSettings(database_url="postgresql+psycopg://x@localhost/x")  # type: ignore[arg-type]


def principal() -> Principal:
    return Principal(id="fake:ed", type="user", display="Ed", issuer="x", subject="ed", via="jwt")


def item_in(document_b64: str) -> ValidationBatchItemIn:
    return ValidationBatchItemIn(
        contract_id=CONTRACT,
        folder_id=FOLDER,
        media_type="application/json",
        document_b64=document_b64,
    )


def test_items_decode_to_their_raw_bytes() -> None:
    encoded = base64.b64encode(b'{"kind": "DataContract"}').decode()
    [decoded] = decode_items([item_in(encoded)], settings())
    assert decoded.raw == b'{"kind": "DataContract"}'
    assert (decoded.contract_id, decoded.folder_id) == (CONTRACT, FOLDER)


def test_a_bad_base64_value_is_a_422_naming_its_position_without_echoing_it() -> None:
    good = base64.b64encode(b"{}").decode()
    with pytest.raises(ProblemError) as caught:
        decode_items([item_in(good), item_in("not*base64!")], settings())
    problem = caught.value
    assert (problem.status, problem.slug) == (422, "validation-error")
    assert problem.extensions["errors"] == [
        {"pointer": "/items/1/document_b64", "detail": "document_b64 is not valid base64."}
    ]
    assert "not*base64!" not in str(problem.extensions)


class DownAuthorizer:
    async def check(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AuthorizerUnavailable("identity is down")


async def test_an_authorizer_outage_is_a_503_never_an_unchecked_folder() -> None:
    with pytest.raises(ProblemError) as caught:
        await folder_facts(DownAuthorizer(), principal(), "ruleset:dry-run", [FOLDER], FOLDER)
    assert (caught.value.status, caught.value.slug) == (503, "service-unavailable")


def resolved() -> ResolvedRuleSet:
    return ResolvedRuleSet(
        rule_set_id=uuid.uuid4(),
        rule_set_version_id=uuid.uuid4(),
        name="Org naming",
        scope="folder",
        folder_id=FOLDER,
        folder_kind="organization",
        depth=1,
        version_no=2,
        dsl_version="cel-1",
        source="mandatory",
        mode="enforce",
        enforce_at=None,
    )


def prepared() -> _Prepared:
    rule = RuleDef.model_validate(
        {
            "key": "has-schema",
            "target": "$",
            "condition": "has(this.schema)",
            "severity": "error",
            "message": "A contract needs a schema.",
            "fixtures": {"pass": [], "fail": []},
        }
    )
    entry = resolved()
    return _Prepared((entry,), [], {0: RuleBinding(entry, rule)})


def test_a_unit_without_a_verdict_is_an_engine_error_at_the_document_root() -> None:
    [verdict] = _verdicts(prepared(), {})
    assert verdict.outcome == "error"
    assert verdict.error_code == "engine_error"
    assert verdict.blocking is True
    assert (verdict.pointers, verdict.pointers_total) == ([""], 1)


def test_a_passing_unit_is_not_a_failing_rule() -> None:
    assert _verdicts(prepared(), {0: UnitResult(id=0, outcome="pass")}) == []


async def test_a_document_that_does_not_parse_has_no_verdicts_and_echoes_nothing() -> None:
    item = BatchItem(
        contract_id=CONTRACT, folder_id=FOLDER, media_type="application/json", raw=b"{not json"
    )
    app_state = SimpleNamespace(settings=settings())
    rules = await _evaluate_item(app_state, principal(), None, None, item, prepared())  # type: ignore[arg-type]
    assert rules is None


class NoRowSession:
    """A session whose every query finds no row (the set was deleted after the policy check)."""

    async def execute(self, _statement: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: None)


async def test_a_rule_set_gone_after_the_policy_check_is_the_uniform_404() -> None:
    with pytest.raises(ProblemError) as caught:
        await _load_dry_run_version(NoRowSession(), uuid.uuid4(), "draft")  # type: ignore[arg-type]
    assert (caught.value.status, caught.value.slug) == (404, "not-found")
