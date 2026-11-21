---
name: contract-lifecycle
description: Change the contract version lifecycle (drafts, publish, Latest, promote, unpublish, deprecate, consumer fetch, delete guard) without breaking immutability, the lock order or the one-Latest invariant.
---

# contract-lifecycle

Use when touching `contracts/src/dc_contracts/{versions,drafts,lifecycle,notation,http_cache,references,compat,link_facts,diffs,impact}.py`,
`api/drafts.py`, `api/versions.py`, or migration `0005_contract_version_lifecycle.py` and later.
The parsing, storage and audit rules of `contract-ingest` (`../contract-ingest/SKILL.md`) still
apply; this skill does not repeat them.

1. **One table, three states.** Everything lives in `contract_version`: `draft -> published <->
   unpublished`. The draft becomes the version in place, same row and same id (D-51); there is no
   copy and no second table. A new draft is a new row (`versions.create_draft`).
2. **Immutability is the database's job.** `trg_contract_version_guard` allows a published row to
   change only the columns in its `publish_mutable` / `lifecycle_mutable` allow-lists; **a new
   column is immutable after publish unless a new migration adds it to those lists.**
   `trg_contract_version_no_delete` refuses every delete of a row. Never rely on application
   checks alone and never write a migration that drops either trigger.
3. **Latest is a pointer plus a flag.** `contract.latest_version_id` and `contract_version.is_latest`
   agree, enforced by `uq_contract_version_one_latest` (a partial unique index) and the deferred
   constraint trigger `trg_contract_version_latest_consistent`. Status
   (`latest`/`preview`/`older`) is **derived** (`lifecycle.derive_status`) and never stored;
   deprecation is a separate flag (`deprecated_at`, `sunset_date`), orthogonal to status.
4. **Lock order for every lifecycle write** (`versions.lock_contract`, `lock_version`):
   the contract row first (`FOR NO KEY UPDATE`; `FOR UPDATE` for delete), then the version or
   draft rows, then referenced contracts (`FOR KEY SHARE`, sorted by id, `references.index_references`).
   Every locked read uses `populate_existing` so a stale identity-map copy cannot hide a
   concurrent change. The Latest swap is **clear the old `is_latest`, flush, then set the new
   one**; reversing it trips the unique index. One `uow.audit(...)` per operation, metadata only.
5. **Semver rules.** `lifecycle.validate_publish`: the declared `version` must be a quoted SemVer
   2.0.0 **string** (422 `invalid-version`), strictly greater than every non-draft row,
   published or unpublished, by precedence (409 `semver-conflict`); build metadata is ignored; a
   number is never reused after an unpublish. Core numbers above `MAX_SEMVER_PART` are refused.
6. **Publish order** (`versions.publish_draft`): `If-Match` first (428 without), then the gate on
   the exact stored bytes **before any lock** via `versions.get_publish_gate(app.state).check(raw,
   media_type, settings, ctx)` (`ctx` is the `GateContext`: contract and draft ids, the folder
   facts, the caller's own bearer, the attachments and their revision; the answer is a `GateResult`
   with the parsed document, `run_id`, the pins, `engine_version`, `schema_sha` and the echoed
   revision), then the locks, then a re-check of revision and
   content hash under the lock (an edit during the gate is a 412). The real gate is
   `validation_client.ValidationPublishGate`, installed unconditionally by `validation_hook`; with
   no gate the fallback is `FailClosedGate` (503), never a pass. `PassThroughPublishGate` is a test
   double only. Step (h) writes `validation_run_id`, `pinned_rule_sets`, `engine_version` and
   `schema_sha` from the result in the publish transaction (the guard function names them in
   `publish_mutable`, so nothing can change them later: **a new pin column must be added to that list
   in a new migration** that replaces the guard function, as migration 0014 did, or the publish
   transaction is refused). Every outcome without a verdict is fail-closed: 503
   `validation-unavailable` for an outage, a timeout, a 5xx or a hash that differs from the gated
   one, and 422 `contract-validation-failed` (report and `run_id`) for a verdict that is not
   publishable; the draft is untouched and no lifecycle event is written. **A 422
   `contract-validation-failed` (and only that slug: never a 503, 409, 412, 428 or 404) is audited
   (D-175):** `api/drafts.py` catches it around `publish_draft`, and `_record_refused_publish` rolls
   the refused unit back, then records one `contract.version.publish_refused` event on the contract
   in its own transaction (`scope_path` = the contract's `ancestor_ids`). Its `after` holds only
   `draft_id`, `semver` (the draft's declared version, at most 256 characters), `run_id` (a UUID or
   null) and `blocking`: at most 50 `{rule_key, code}` pairs from the report's `blocking: true`
   findings in report order (`code` is the finding's `error_code`, else its `outcome`; a
   structurally invalid document adds `{rule_key: odcs-json-schema, code: structural}` first).
   Never a finding message, pointer, path or document value. A failure while recording is logged
   (`publish_refused_not_recorded`, ids and error class only) and swallowed, and the original
   `ProblemError` is re-raised unchanged, so the client always gets the gate's 422 body. The notice
   consumer ignores the action. Step (a) also reads the contract's explicit rule-set attachments and
   `attachments_revision` (`attachments.attachments_for_gate`, unlocked) and sends them with the gate;
   step (e2), under the lock, answers 409 `rules-changed` when `contract.attachments_revision` differs
   from the one the gate validated. Attach and detach take the same `lock_contract`
   (`locks.py`) and never run while a network call is open. Do not add rule evaluation anywhere else
   on the publish path.
6a. **Baseline, floor and link facts (Phase 6, VAL-02, VAL-03).** In the same unlocked block as the
   attachments, step (a) reads `compat.baseline_and_floor` (the highest published version by
   SemVer precedence, Preview, Latest and deprecated included, never unpublished rows or drafts; the
   floor is the highest major among published non-prerelease versions) and
   `link_facts.resolve_link_facts` (the document's contract links as the caller sees them: a target
   the caller cannot read is `not_found` like a missing one; publish resolves them with
   `strict=True`, so an identity outage is 503 rather than `unverifiable` links that can never fail
   a rule, while save-mode runs stay advisory, WR-02), and sends `baseline`,
   `stable_major_floor` and `ref_facts` with the gate. Validation decides: a breaking change without
   a declared major above the floor is the native blocking finding `semver-breaking-change`, so
   publish answers 422 `contract-validation-failed`; there is no override, field, header or role
   around it. Under the contract lock, after the SemVer checks, `compat.baseline_key` is read again;
   a different baseline id or floor is **409 `baseline-changed`** and nothing is published (the
   retry validates against the new baseline). After `index_references` (which locks the link
   targets), `link_facts.recheck_link_facts` locks the targets' non-draft version rows `FOR KEY
   SHARE` and recomputes the link facts with the scopes the resolver read before the gate (never an
   identity call under the lock); a fact that got worse (target version unpublished, target
   deleted) is the same 409 `baseline-changed`, a better one is accepted so mutually linking
   publishes still both succeed (IN-03). The
   resolver returns a `LinkFacts` (facts plus those inputs); save-mode runs send only `.facts`. Save-mode runs (draft saves and
   `POST .../drafts/{draft_id}/validate`) send the same baseline, floor and link facts, so a saved
   draft's report already says which version publish needs. The publish event also carries
   `notify_pins` (`[{card_id, version_id}]`, read under the locks) for the
   `contract.version.breaking_published` notice (see `subscriber-cards`).
7. **Where guards go (implemented in Phase 4.1).** The pin guard sits inside
   `versions.unpublish_version` and `versions.delete_contract`, after the contract lock and
   before the write, so a concurrent subscribe cannot slip in (subscribe takes the same lock).
   Each guard **collects every blocker, then raises one `LifecycleBlocked`** (a `ProblemError`,
   409, slug `reasons[0].code`, full list in `reasons`). Reason order (D-106): unpublish lists
   `latest-version-protected` then `version-pinned`; delete lists `contract-referenced` then
   `contract-pinned`. A refused unpublish rolls its unit back, then writes
   `contract.version_unpublish_blocked` on the same session, only when pinned cards exist (never
   inside the locked unit); a blocked delete records nothing. Publish, promote and deprecate
   events carry `notify_card_ids`, read after their locks with one indexed read; the notices
   themselves are materialised later by the consumer. See `subscriber-cards`
   (`../subscriber-cards/SKILL.md`).
8. **Notation specifics stay behind the adapter.** `notation.get_adapter(notation)` owns
   `declared_version`, `starter`, `copy_with` and `references`; `OdcsAdapter` is the only
   implementation. Lifecycle code never inspects ODCS fields itself.
9. **Visibility.** Drafts need `contract:read`; unpublished versions need `contract:publish`;
   published versions need `contract:read-published` (`versions.visible_states`). A caller who
   holds only published access never learns of a draft-only contract (the unknown-contract 404,
   not an empty list). `GET /contracts/{id}` needs `contract:view` and shows published-only
   callers the Latest only. Write routes address versions by SemVer only; `latest` is read-only.
10. **Conditional requests.** Draft ETag `"r<revision>"` with strong `If-Match`; version ETag
    `"<sha256>"` (`-json`/`-yaml` for conversions) with weak `If-None-Match`; the 304 carries the
    same `ETag`, `Vary`, `Cache-Control` and the `Deprecation`/`Sunset` headers
    (`http_cache.py`). Revalidation runs after authorization and visibility, never before.
10a. **Report fetch and changelog (Phase 6, VAL-09).** `GET .../versions/{ref}/validation-report`
    uses the version fetch's policy and visibility, then reads the version's `validation_run_id`
    and calls `ValidationClient.get_run` with the caller's bearer (validation re-authorises the run
    read); no run is 404 `validation-report-not-recorded`; ETag `"run-<run_id>"`. `GET
    .../contracts/{contract_id}/diff?from=&to=` (`diffs.py`) resolves each ref (SemVer, `latest`,
    or a draft id for `contract:read` holders) with the same visibility rules, classifies in a
    thread with `dc_odcs.diff`, pages with an opaque offset cursor and stores nothing. Neither route
    changes state or writes an audit event.
10b. **Governance reads (Phase 6, RULE-10, RULE-11).** `api/governance.py` and `impact.py`: `POST
    /governance/rule-set-dry-runs` and `GET /governance/drift?folder_id=` page the stored Latest
    documents of in-scope contracts (keyset by contract id, cut by `DC_IMPACT_PAGE_SIZE`,
    `DC_IMPACT_PAGE_MAX_FOLDERS` and `DC_IMPACT_PAGE_MAX_BYTES`) to validation's batch routes with
    the caller's bearer. They take no lifecycle lock, change no state, show names only inside the
    caller's `contract:read-metadata` scope and never return document content. JSON Pointers are
    content-derived (an author-controlled target locates values), so `impact.visible_rules`
    returns them only inside the caller's `contract:read` scope (PDP, 503 on an outage) and
    otherwise empties them with `pointers_withheld: true`. Each answered page writes one
    counts-only audit event (`governance.rule_set_dry_run` / `governance.drift_read`) after the
    evaluation; never put a contract id, rule key or pointer into it (CR-01, AR-6-03).
11. **Delete guard.** A soft delete is refused (409 `contract-referenced`) while a published
    version of any other non-deleted contract links to it, in any Organization
    (`references.blocking_referrers`); referrers the caller cannot see are counted in
    `hidden_referrer_count` and never named. The reverse direction (WR-03): `republish_version`
    and `restore_contract` call `references.deleted_link_targets` under their contract lock (it
    locks live targets `FOR KEY SHARE` like `index_references`) and refuse with 409
    `contract-referenced` (`deleted_target_count`, never named) when a stored link of the version,
    or of any published version of the restored contract, resolves to a soft-deleted contract.
12. **Prove changes.** `uv run pytest -m integration contracts/tests/integration/test_version_immutability_db.py`,
    and `contracts/tests/integration/test_version_concurrency.py` and
    `contracts/tests/integration/test_pin_concurrency.py`, each three consecutive runs (a race that
    passes once is not proof); the `INVARIANT_SQL` check in `contracts/tests/integration/conftest.py`
    must return no rows after every scenario. End to end:
    `bash scripts/e2e.sh tests/e2e/test_phase3_lifecycle_journey.py`, and for compatibility,
    reports, diffs or breaking notices `tests/e2e/test_phase6_semantic_journey.py`.
