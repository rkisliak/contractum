"""The rule model: the public, persisted shape of a rule and a rule set (D-110).

Every model forbids extra fields. There is deliberately no field that configures what an
evaluation error means: an error is never a pass and never a fail (D-110, D-139).

Byte and length limits that are deployment settings (`dc_rules.ports.Limits`) are applied by the
rule check, not here; the model keeps only hard sanity caps so a hostile body cannot allocate an
unbounded string.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

DslVersion = Literal["cel-1", "cel-2"]
# The default for every new draft. cel-1 is a legacy variant kept so published (immutable) rule-set
# versions evaluate exactly as before (D-152).
DSL_VERSION: DslVersion = "cel-2"

# Why a run happens (cel-2 only, bound as `ctx.operation`): a draft save or validation is `save`;
# the publish gate (and the batch evaluations that judge stored published versions) is `publish`.
Operation = Literal["save", "publish"]
# The `ctx` keys only a cel-2 unit sees; the worker strips them for cel-1 so legacy versions
# evaluate exactly as before.
CEL2_CTX_KEYS: Final = frozenset({"operation"})

Severity = Literal["error", "warning", "info"]
MatchMode = Literal["each", "any"]
OnEmpty = Literal["pass", "fail"]

# Hard sanity caps (not the Limits settings).
MAX_CONDITION_CHARS = 100_000
MAX_TARGET_CHARS = 10_000

JsonValue = Any


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FixtureCtx(_Strict):
    folder_kind: Literal["root", "organization", "project", "team"] = "team"
    folder_path: str = "fixture"
    organization_id: str | None = None
    # cel-2 only. Absent values never enter canonical JSON, so existing content hashes hold.
    operation: Operation | None = Field(default=None, exclude_if=lambda v: v is None)


class Fixture(_Strict):
    description: str | None = Field(default=None, max_length=1000)
    document: JsonValue
    ctx: FixtureCtx | None = None
    # cel-2 only: the baseline document and the reference facts the rule sees (D-152). The
    # classified changes are derived from (previous, document) by the caller's `changes_for`.
    previous: JsonValue | None = None
    refs: dict[str, JsonValue] | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_cel2_inputs(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Absent previous/refs are not serialised, so the canonical JSON (and the content hash)
        # of every cel-1 era rule set stays byte-identical.
        data: dict[str, Any] = handler(self)
        for name in ("previous", "refs"):
            if data.get(name) is None:
                data.pop(name, None)
        return data


class RuleFixtures(_Strict):
    pass_: list[Fixture] = Field(alias="pass")
    fail: list[Fixture]


RuleKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]


class RuleDef(_Strict):
    key: RuleKey
    description: str | None = Field(default=None, max_length=1000)
    target: str = Field(default="$", min_length=1, max_length=MAX_TARGET_CHARS)
    condition: str = Field(min_length=1, max_length=MAX_CONDITION_CHARS)
    severity: Severity
    message: str = Field(min_length=1, max_length=500)
    match: MatchMode = "each"
    on_empty: OnEmpty = "pass"
    fixtures: RuleFixtures


class RuleSetContent(_Strict):
    dsl_version: DslVersion = DSL_VERSION
    rules: list[RuleDef]

    @model_validator(mode="after")
    def _cel1_fixtures_have_no_cel2_inputs(self) -> RuleSetContent:
        if self.dsl_version != "cel-1":
            return self
        for rule in self.rules:
            for fixture in (*rule.fixtures.pass_, *rule.fixtures.fail):
                if fixture.previous is not None or fixture.refs is not None:
                    raise ValueError(
                        f"rule '{rule.key}': fixtures with previous or refs need dsl_version cel-2"
                    )
                if fixture.ctx is not None and fixture.ctx.operation is not None:
                    raise ValueError(
                        f"rule '{rule.key}': fixtures with ctx.operation need dsl_version cel-2"
                    )
        return self


def canonical_json(value: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, no whitespace, UTF-8, no ASCII escaping."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def content_sha256(content: RuleSetContent) -> str:
    """Hex SHA-256 of the canonical JSON of the content (by alias, JSON mode)."""
    return hashlib.sha256(
        canonical_json(content.model_dump(mode="json", by_alias=True))
    ).hexdigest()
