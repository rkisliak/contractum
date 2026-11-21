"""Fail-closed edges of the request parsers and the sandbox seams, without a database (D-137).

The routes are covered end to end by the integration suite; these are the branches a real stack
cannot reach on demand: a sandbox that will not start, a stored DSL this engine does not know, a
body that is not text, and a problem the advisory check must not swallow.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from dc_core.problems import ProblemError
from dc_rules.model import RuleDef, RuleSetContent
from dc_rules.ports import Limits
from dc_rules.protocol import EvalUnit
from dc_validation.api import rulesets as rulesets_api
from dc_validation.api.runs import _document_bytes
from dc_validation.resolution import ResolvedRuleSet
from dc_validation.rulesets import run_check
from dc_validation.runs import ValidationRunIn, _evaluate, _require_known_dsl, decode_request
from dc_validation.sandbox import SandboxBusy, SandboxUnavailable
from dc_validation.settings import ValidationSettings

pytestmark = pytest.mark.unit

RULE = {
    "key": "has-schema",
    "target": "$",
    "condition": "has(this.schema)",
    "severity": "error",
    "message": "A contract needs a schema.",
    "fixtures": {"pass": [{"document": {"schema": []}}], "fail": [{"document": {}}]},
}


def settings() -> ValidationSettings:
    return ValidationSettings(database_url="postgresql+psycopg://x@localhost/x")  # type: ignore[arg-type]


def assert_problem(caught: pytest.ExceptionInfo[ProblemError], status: int, slug: str) -> None:
    assert (caught.value.status, caught.value.slug) == (status, slug)


class FailingSandbox:
    """A sandbox whose every call raises ``error``."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def check(self, units: Any) -> Any:
        raise self.error

    async def evaluate(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error


# --- the sandbox seams -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "retry_after"),
    [(SandboxBusy("saturated"), {"Retry-After": "1"}), (SandboxUnavailable("no worker"), None)],
)
async def test_a_check_the_sandbox_cannot_run_is_503_never_a_pass(
    error: Exception, retry_after: dict[str, str] | None
) -> None:
    content = RuleSetContent.model_validate({"rules": [RULE]})
    with pytest.raises(ProblemError) as caught:
        await run_check(FailingSandbox(error), content, Limits())  # type: ignore[arg-type]
    assert_problem(caught, 503, "validation-unavailable")
    assert caught.value.headers == retry_after


async def test_an_evaluation_the_sandbox_cannot_start_is_503_without_retry_after() -> None:
    units = [EvalUnit(id=0, rule=RuleDef.model_validate(RULE))]
    with pytest.raises(ProblemError) as caught:
        await _evaluate(FailingSandbox(SandboxUnavailable("no worker")), units, {}, {})  # type: ignore[arg-type]
    assert_problem(caught, 503, "validation-unavailable")
    assert caught.value.headers is None


def resolved(dsl_version: str) -> ResolvedRuleSet:
    return ResolvedRuleSet(
        rule_set_id=uuid.uuid4(),
        rule_set_version_id=uuid.uuid4(),
        name="Future rules",
        scope="folder",
        folder_id=uuid.uuid4(),
        folder_kind="organization",
        depth=1,
        version_no=1,
        dsl_version=dsl_version,
        source="mandatory",
        mode="enforce",
        enforce_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_a_stored_dsl_this_engine_does_not_know_is_refused_not_evaluated() -> None:
    _require_known_dsl(resolved("cel-2"))  # a known DSL passes silently
    with pytest.raises(ProblemError) as caught:
        _require_known_dsl(resolved("cel-99"))
    assert_problem(caught, 503, "validation-unavailable")


def advisory_request(monkeypatch: pytest.MonkeyPatch, slug: str) -> Any:
    """A request whose rule-set check raises a 503 problem with ``slug``."""

    async def refusing_check(*_args: Any) -> Any:
        raise ProblemError(503, slug, "Service Unavailable")

    monkeypatch.setattr(rulesets_api, "run_check", refusing_check)
    state = SimpleNamespace(settings=settings(), sandbox=None)
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_the_advisory_check_reports_an_unavailable_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = advisory_request(monkeypatch, "validation-unavailable")
    assert await rulesets_api._advisory_check(request, [RULE]) == {"status": "unavailable"}


async def test_the_advisory_check_never_swallows_another_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = advisory_request(monkeypatch, "service-unavailable")
    with pytest.raises(ProblemError) as caught:
        await rulesets_api._advisory_check(request, [RULE])
    assert_problem(caught, 503, "service-unavailable")


# --- request parsing -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "content_type", "detail"),
    [
        (b"", "application/json", "The request body is empty"),
        (b"\xff\xfe{}", "application/json", "The document is not valid UTF-8 (byte offset 0)"),
        (b"[1, 2]", "application/json", "The document root must be a mapping"),
        (b"- a\n- b\n", "application/yaml", "The document root must be a mapping"),
    ],
)
async def test_a_draft_body_that_is_not_a_mapping_of_text_is_422_malformed(
    raw: bytes, content_type: str, detail: str
) -> None:
    with pytest.raises(ProblemError) as caught:
        await rulesets_api._parse_mapping(raw, content_type)
    assert_problem(caught, 422, "malformed-document")
    assert caught.value.detail == detail


async def test_invalid_json_in_a_draft_is_422_malformed_naming_json() -> None:
    with pytest.raises(ProblemError) as caught:
        await rulesets_api._parse_mapping(b'{"rules": [}', "application/json; charset=utf-8")
    assert_problem(caught, 422, "malformed-document")
    assert str(caught.value.detail).startswith("Invalid JSON: ")


async def test_a_parser_recursion_error_is_422_malformed_never_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def too_deep(_text: str) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(rulesets_api, "load_json", too_deep)
    with pytest.raises(ProblemError) as caught:
        await rulesets_api._parse_mapping(b'{"rules": []}', "application/json")
    assert_problem(caught, 422, "malformed-document")
    assert caught.value.detail == "Invalid JSON: nesting is too deep"


def test_an_enveloped_document_that_is_not_text_is_422_and_one_over_the_cap_413() -> None:
    with pytest.raises(ProblemError) as caught:
        _document_bytes("\ud800", 1024)  # a lone surrogate has no UTF-8 form
    assert_problem(caught, 422, "validation-error")
    assert caught.value.extensions["errors"] == [
        {"pointer": "/document", "detail": "The document is not valid text."}
    ]
    with pytest.raises(ProblemError) as too_large:
        _document_bytes("x" * 11, 10)
    assert_problem(too_large, 413, "payload-too-large")
    assert _document_bytes("ok", 10) == b"ok"


@pytest.mark.parametrize(
    ("missing", "pointer"),
    [("contract_id", "/contract_id"), ("draft_id", "/draft_id")],
)
def test_a_publish_run_without_its_ids_is_a_422_naming_the_field(
    missing: str, pointer: str
) -> None:
    fields: dict[str, Any] = {
        "mode": "publish",
        "contract_id": uuid.uuid4(),
        "draft_id": uuid.uuid4(),
        "media_type": "application/json",
        "document_b64": base64.b64encode(b"{}").decode(),
    }
    fields[missing] = None
    with pytest.raises(ProblemError) as caught:
        decode_request(ValidationRunIn.model_validate(fields))
    assert_problem(caught, 422, "validation-error")
    assert caught.value.extensions["errors"][0]["pointer"] == pointer
