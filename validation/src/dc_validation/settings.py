"""Validation service settings (``DC_`` environment variables plus Docker secret files)."""

from typing import Self

from dc_core.settings import CoreSettings
from dc_rules.ports import Limits
from pydantic import Field, model_validator


class ValidationSettings(CoreSettings):
    """Sandbox sizing and the rule limits (every `Limits` field is a setting of the same name)."""

    service: str = "validation"

    # Sandbox: bounded worker processes (D-137, D-138).
    eval_workers: int = Field(default=2, ge=1)
    max_concurrent_runs: int = Field(default=4, ge=1)
    queue_wait_s: float = Field(default=2.0, ge=0)
    rule_timeout_ms: int = Field(default=500, ge=1)
    run_budget_s: float = Field(default=5.0, gt=0)
    # A rule-set check runs up to 12 units per rule, so it gets three times the run budget.
    check_budget_s: float = Field(default=15.0, gt=0)
    # 1 GiB: measured against cel-expr-python 0.1.4 in python:3.13-slim-trixie (plan 05-03).
    worker_memory_bytes: int = Field(default=1_073_741_824, ge=64 * 1024 * 1024)
    worker_max_jobs: int = Field(default=200, ge=1)
    # How long a fresh worker may take to import the engine and say it is ready (IN-04).
    worker_ready_timeout_s: float = Field(default=5.0, gt=0)

    # Rule impact batches (D-161, WR-03): a dry-run or drift page holds one batch slot and
    # evaluates one document at a time, each holding a run slot and a worker only while it runs.
    # The scarce resource is the worker, so the pool is kept below both the workers and the run
    # slots: unset, it is half the run slots capped at ``eval_workers - 1`` (at least one); a
    # configured pool that could hold every worker or every run slot is refused at start-up, so
    # at least one worker is always left for save and publish runs. With a single worker
    # (development and tests only) that worker is necessarily shared.
    # A page is capped by documents, total decoded bytes and distinct folders (each folder costs
    # one PDP check). Unmeasured starting points (research A9), tune here. Contracts sizes its
    # pages against these defaults (``dc_contracts.settings.VALIDATION_BATCH_CAPS``, IN-06): lower
    # a cap here only together with the matching DC_IMPACT_PAGE_* on contracts, or every impact
    # page is refused (422 here, 503 there).
    batch_slots: int | None = Field(default=None, ge=1)
    # Wall-clock budget of one page (WR-03). Past it, or once the caller has disconnected, the
    # page stops evaluating and answers 503 ``validation-unavailable``. Keep it below contracts'
    # ``DC_VALIDATION_IMPACT_TIMEOUT_S`` (30), which gives up on the page anyway.
    batch_page_budget_s: float = Field(default=25.0, gt=0)
    max_batch_items: int = Field(default=25, ge=1)
    max_batch_bytes: int = Field(default=4 * 1024 * 1024, ge=1)
    max_batch_folders: int = Field(default=20, ge=1)

    # Largest rule-set draft body the authoring route reads (the gateway body limit is 2 MiB).
    max_draft_bytes: int = Field(default=2_000_000, ge=1)

    # Validation runs (D-125, D-126): the largest document a run parses (decoded bytes), the
    # findings a report lists (verdict counts are computed before the cap) and the attachments
    # one request may carry.
    max_document_bytes: int = Field(default=1_048_576, ge=1)
    max_findings: int = Field(default=200, ge=1)
    max_attachments: int = Field(default=50, ge=0)
    # Ad-hoc rule sets (``?rule_set=``) one on-demand upload may try (D-126).
    max_adhoc_rule_sets: int = Field(default=10, ge=0)

    # Reference facts (D-153): the references resolved per kind in one run (the rest are
    # unverifiable) and how long identity's answers about people and teams are remembered.
    max_refs_per_kind: int = Field(default=200, ge=1)
    principal_cache_ttl_s: float = Field(default=45.0, ge=0)

    # D-120: warn period before a newly mandatory version is enforced; 0 enforces immediately.
    mandatory_warn_days: int = Field(default=14, ge=0)

    # Rule limits (same names as `dc_rules.ports.Limits`).
    max_expression_bytes: int = Field(default=2048, ge=1)
    max_target_chars: int = Field(default=512, ge=1)
    max_rules_per_set: int = Field(default=200, ge=1)
    max_compiled_bytes: int = Field(default=32768, ge=1)
    max_matches: int = Field(default=10_000, ge=1)
    max_matches_with_contract: int = Field(default=100, ge=1)
    max_fixtures_per_kind: int = Field(default=5, ge=1)
    max_fixture_bytes: int = Field(default=65536, ge=1)

    def batch_slot_count(self) -> int:
        """The batch pool: ``batch_slots``, or half the run slots capped at ``eval_workers - 1``.

        Never below one (WR-03).
        """
        if self.batch_slots is not None:
            return self.batch_slots
        return max(1, min(self.max_concurrent_runs // 2, self.eval_workers - 1))

    @model_validator(mode="after")
    def _batches_leave_room_for_runs(self) -> Self:
        """Refuse a batch pool that could hold every worker or every run slot (WR-03)."""
        slots = self.batch_slot_count()
        if self.eval_workers > 1 and slots >= self.eval_workers:
            raise ValueError(
                "batch_slots must be below eval_workers, so a worker is always left for runs"
            )
        if self.max_concurrent_runs > 1 and slots >= self.max_concurrent_runs:
            raise ValueError(
                "batch_slots must be below max_concurrent_runs, so a run slot is always left"
            )
        return self

    def limits(self) -> Limits:
        return Limits(
            max_expression_bytes=self.max_expression_bytes,
            max_target_chars=self.max_target_chars,
            max_rules_per_set=self.max_rules_per_set,
            max_compiled_bytes=self.max_compiled_bytes,
            max_matches=self.max_matches,
            max_matches_with_contract=self.max_matches_with_contract,
            max_fixtures_per_kind=self.max_fixtures_per_kind,
            max_fixture_bytes=self.max_fixture_bytes,
        )
