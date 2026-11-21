"""Pure helpers behind the contract routes: request models, refusal records, the test-email limiter
and create-from-template's re-serialisation.

* The create bodies refuse a NUL in an ODCS id or source version (PostgreSQL cannot store it).
* A refused publish records identifiers only: a report that is not an object records no
  findings, and a run id that is not a UUID is recorded as null (D-175).
* The test-email limiter enforces the per-process window across principals, frees slots as the
  window slides, and forgets idle principals once it tracks more than 1000.
* ``build_from_template`` strips a template's byte-order mark and refuses an override that cannot
  be stored as UTF-8.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from dc_contracts.api import settings as settings_api
from dc_contracts.api.contracts import (
    ContractFromTemplateIn,
    ContractFromVersionIn,
    ContractStarterIn,
)
from dc_contracts.api.drafts import _refused_blocking, _refused_run_id
from dc_contracts.api.settings import TestEmailLimiter
from dc_contracts.templates import build_from_template
from dc_core.problems import ProblemError
from pydantic import ValidationError

pytestmark = pytest.mark.unit

NUL_ID = "urn:dc:a\x00b"


@pytest.mark.parametrize(
    "build",
    [
        lambda: ContractFromTemplateIn(template_id=uuid.uuid4(), odcs_id=NUL_ID),
        lambda: ContractStarterIn(odcs_id=NUL_ID, name="Orders"),
        lambda: ContractFromVersionIn(source_contract_id=uuid.uuid4(), odcs_id=NUL_ID),
        lambda: ContractFromVersionIn(
            source_contract_id=uuid.uuid4(), source_version="1.0.0\x00", odcs_id="urn:dc:x"
        ),
    ],
    ids=["from-template", "starter", "from-version-id", "from-version-semver"],
)
def test_a_nul_in_a_create_body_is_refused(build: object) -> None:
    with pytest.raises(ValidationError, match=r"NUL \(U\+0000\) characters are not allowed"):
        build()  # type: ignore[operator]


@pytest.mark.parametrize("report", [None, "not a report", ["finding"]])
def test_a_refusal_report_that_is_not_an_object_records_no_findings(report: object) -> None:
    assert _refused_blocking(report) == []


def test_a_refusal_run_id_is_recorded_only_as_a_uuid() -> None:
    run_id = uuid.uuid4()
    assert _refused_run_id(str(run_id)) == str(run_id)
    assert _refused_run_id(None) is None
    assert _refused_run_id("run-42; drop table") is None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(settings_api.time, "monotonic", fake)
    return fake


def test_the_per_process_window_refuses_a_fresh_principal_until_it_slides(clock: Clock) -> None:
    limiter = TestEmailLimiter()
    for n in range(settings_api.PER_PROCESS_LIMIT):
        assert limiter.check(f"user-{n}") is None

    wait = limiter.check("someone-new")
    assert wait == int(settings_api.PER_PROCESS_WINDOW_S) + 1

    clock.now += settings_api.PER_PROCESS_WINDOW_S
    assert limiter.check("someone-new") is None


def test_the_per_principal_window_frees_a_slot_as_it_slides(clock: Clock) -> None:
    limiter = TestEmailLimiter()
    for _ in range(settings_api.PER_PRINCIPAL_LIMIT):
        assert limiter.check("alice") is None
        clock.now += 1.0
    assert limiter.check("alice") is not None

    clock.now += settings_api.PER_PRINCIPAL_WINDOW_S - settings_api.PER_PRINCIPAL_LIMIT + 1.0
    assert limiter.check("alice") is None


def test_idle_principals_are_forgotten_once_more_than_1000_are_tracked(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_api, "PER_PROCESS_LIMIT", 10_000)
    limiter = TestEmailLimiter()
    for n in range(1000):
        assert limiter.check(f"user-{n}") is None
    clock.now += settings_api.PER_PRINCIPAL_WINDOW_S  # every earlier send leaves its window
    for n in range(10):
        limiter.check(f"user-{n}")  # trimmed to empty, then counted again

    assert limiter.check("one-more") is None

    tracked = limiter._principals
    assert set(tracked) == {*(f"user-{n}" for n in range(10)), "one-more"}


def template_version(raw: bytes, media_type: str = "application/yaml") -> SimpleNamespace:
    return SimpleNamespace(raw_document=raw, media_type=media_type)


def test_build_from_template_strips_a_bom_and_sets_the_new_id() -> None:
    raw = "﻿apiVersion: v3.2.0\nkind: DataContract\nid: tpl\nname: T\n".encode()

    built = build_from_template(
        template_version(raw),  # type: ignore[arg-type]
        odcs_id="urn:dc:new",
        overrides={"name": "Orders"},
    )

    assert built == (
        b'{\n  "apiVersion": "v3.2.0",\n  "kind": "DataContract",\n  "id": "urn:dc:new",\n'
        b'  "name": "Orders"\n}\n'
    )


def test_build_from_template_refuses_an_override_that_is_not_utf_8() -> None:
    raw = b"apiVersion: v3.2.0\nkind: DataContract\nid: tpl\n"

    with pytest.raises(ProblemError) as exc:
        build_from_template(
            template_version(raw),  # type: ignore[arg-type]
            odcs_id="urn:dc:new",
            overrides={"name": "bad \ud800 surrogate"},
        )

    assert (exc.value.status, exc.value.slug) == (422, "template-override-not-allowed")
