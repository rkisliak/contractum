---
name: contract-ingest
description: Change how contracts are parsed, validated, stored or audited, keeping the dc_odcs pipeline order, byte-faithful storage and metadata-only audit events.
---

# contract-ingest

Use when touching `contracts/src/dc_contracts/{ingest,repo,api/contracts}.py` or
`shared/dc-odcs/src/dc_odcs/`.

1. **Pipeline order** (`dc_odcs.gate.parse_contract`, run through `asyncio.to_thread`): media
   type -> empty body -> size cap -> strict UTF-8 -> parse (`StrictCLoader`, PyYAML) -> mapping
   root -> JSON-native walk (no exotic YAML types, no NUL, no non-finite floats) ->
   `apiVersion`/`kind`/`id` gate. Only these checks and parse errors block a create (D-19).
2. **Structural report is advisory.** `dc_odcs.validate.validate_structure` checks the document
   against the vendored ODCS JSON Schema for its own `apiVersion` (v3.1.0 or v3.2.0, never a
   "latest" URL) and returns a report that goes into the 201 body. A report with errors never
   blocks a create or a save. Governance is a separate, remote step: see item 11 (the advisory
   `validation` embed on save) and the publish gate in `contract-lifecycle`.
3. **Body handling.** `read_body_capped` refuses early on `Content-Length` and aborts the stream
   once `DC_MAX_DOCUMENT_BYTES` is passed (413 `payload-too-large`). The route authenticates and
   authorises before it reads anything.
4. **Raw bytes are the source of truth.** `ContractVersion.raw_document` holds exactly what the
   author sent, with its media type and SHA-256. The document endpoint returns those bytes by
   default; only an explicit `Accept` for the other format returns a converted rendering
   (`convert.py`, not byte-faithful). Never re-serialise the stored document, and never inject
   metadata into it. Derived copies are in-place edits, not re-serialisations:
   `OdcsAdapter.copy_with` (`notation.py`) keeps the source's bytes and media type and replaces
   only the top-level `version` scalar token (a new draft from a version or draft with a bump,
   D-45), so comments, key order, quoting and line endings are kept. The result must strict-parse
   to the source document with only that field changed; a value that is not a single-line plain
   or quoted scalar is refused with 422 `copy-field-not-editable` (never rewritten), the result
   passes the same D-19 gate and size cap, and `bump: none` copies the bytes verbatim.
   Create-from-version (a new contract from a version or draft of another contract, D-44b) is the
   second `copy_with` caller: only the top-level `id` token is replaced by the new ODCS id, the
   declared version, comments and media type are kept, and a source whose `id` is not a
   single-line scalar is refused with 422 `copy-field-not-editable`.
5. **Folder facts come from identity**, not the client: `folder_id`, `ancestor_ids` and
   `organization_id` are taken from the PDP decision. Contracts are created in folders of any
   kind (ROOT, Organization, Project, Team), an archived subtree is read-only (409), and the ODCS id scope
   is `repo.odcs_scope_id` (Organization, or one ROOT scope).
6. **Audit is metadata only.** `uow.audit("contract.created", "contract", ...)` carries ids, the
   ODCS id and version, media type, hash and size. The model's `__audit_fields__` allow-list
   excludes the raw and parsed documents because `customProperties` may hold secrets or personal
   data. Do not add document content to an event.
7. **Templates reuse this pipeline** (`templates.py`, `api/templates.py`). A template is one
   editable draft (`Template.draft_*`, raw bytes, last-write-wins, OQ7) plus numbered immutable
   `TemplateVersion` rows (immutable by database trigger). Create, draft update and publish all go
   through `parse_upload` and `structural_report` and store raw bytes; publish locks the template
   row so version numbers never gap, and audits are metadata only (`template.created`,
   `template.draft_updated`, `template.published`, `template.version_deprecated`). Availability is
   keyed on the target folder's ancestor ids from identity's folder facts, never on the caller's
   grant chain, so a block flag cannot hide a template (D-37).
8. **Create-from-template is the one documented re-serialisation** (`build_from_template`): the
   stored document is the template version's parsed document re-serialised as JSON with the new
   ODCS id and permitted overrides applied; YAML comments are not carried (OQ8). `apiVersion`,
   `kind` and `id` cannot be overridden (`template-override-not-allowed`), a deprecated version is
   refused (`template-version-deprecated`), and the result passes the same D-19 gate and size cap.
   The ODCS starter (D-43, `OdcsAdapter.starter`) is a server-generated document rather than a
   copy of stored content, so there is no source to be faithful to: a dictionary of six keys
   serialised with `yaml.safe_dump`, caller text never interpolated into YAML source, passing the
   same D-19 gate and size cap.
9. **Metadata redaction (D-36) and the head row.** `GET /contracts/{id}` needs `contract:view`
   (every role that can see contracts holds it; a denied view is always 404) and the list needs
   only the principal-level `contract:list`. The head row is the Latest for every caller once one
   exists, else the newest draft (`search.head_version_id`, `repo.get_contract_with_version`);
   newer drafts are read through the drafts routes. A `contract:read-metadata` caller gets the head
   row, with the content-derived fields null and `content_redacted` true unless they hold
   `contract:read`; a published-only caller (`contract:read-published`) gets the Latest only,
   content visible, and the unknown-contract 404 for a contract without one. The list trims with
   three scopes (metadata, content, published; the published one also needs a Latest), and content
   filters match only rows whose content the caller may read. Template reads do the same with
   `draft_redacted` for `template:read-published` callers without `template:read`. Lists are
   trimmed with `scope_clause` over the caller's `FolderScope` (empty 200 page, no totals).
10. **Validation embed on the six draft-producing routes (VAL-06, D-134).** `PUT
    .../drafts/{draft_id}`, `POST .../drafts`, `POST /folders/{folder_id}/contracts` (upload),
    `.../contracts/from-template`, `.../contracts/starter` and `.../contracts/from-version` add a
    `validation` field next to `structural_validation` through
    `validation_client.save_mode_embed`. Call it only **after** the save committed, with the stored
    bytes and media type of the saved row (never the request body), with no lock or transaction open,
    and let it degrade: it never raises and answers exactly `{"status": "unavailable"}` when
    validation is down, slow (`DC_VALIDATION_SAVE_TIMEOUT_S`, 3 s), busy or answers anything but a
    clean 200. A new route that creates or changes draft content must embed the report the same way.
    `POST .../drafts/{draft_id}/validate` (`validate_draft`, `contract:read`) is the explicit
    version: it changes nothing and answers 503 on an outage. Neither stores a run or caches a
    report. Contracts never imports `dc_validation` or `dc_rules`; the report is validation's JSON
    passed through.
11. **Published documents must pass the real gate (D-129).** Every document that a fixture, e2e
    journey or seed publishes goes through validation's ODCS JSON Schema check and the
    `odcs-structural` rules, so a fixture with an invalid or duplicate property is refused with 422
    `contract-validation-failed`. `tests/repo/test_e2e_documents_conform.py` loads each e2e journey's
    `_contract_yaml` and runs the same schema check and seeded rules in process; add a new journey's
    document to its `JOURNEYS` table before it can fail the cold gate.
12. Verify with `uv run pytest -m unit contracts shared/dc-odcs`, then
   `uv run pytest -m integration contracts`, then
   `bash scripts/e2e.sh tests/e2e/test_contracts_flow.py tests/e2e/test_phase_goal_journey.py`.
