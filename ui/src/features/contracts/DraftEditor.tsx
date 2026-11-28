// The draft editor (D-UI-09-10..16, D-UI-10-04..07): Monaco over the stored bytes, Save with
// If-Match (a 412 or a precondition 409 opens the conflict dialog: the server copy and my edits
// side by side, then reload the server copy or keep my edits), Validate (findings at their JSON
// pointers, greyed when the buffer moved on), Discard,
// and Publish with the gate result: blocking findings, warnings, the semver refusal callouts.
// The editor fills the window while it is the last element (a large fixed height once findings
// show below it), and "Expand editor" opens the same buffer full screen with the editing actions.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useBlocker, useNavigate } from "@tanstack/react-router";
import {
  CheckCheck,
  Download,
  GitCompare,
  Rocket,
  Save,
  Trash2,
} from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { isApiError, type Problem } from "@/api/problem";
import type { ContractDraft } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button, ButtonLink } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { CopyButton } from "@/components/CopyButton";
import { Dialog } from "@/components/Dialog";
import { NotFound } from "@/components/NotFound";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { Tooltip } from "@/components/Tooltip";
import {
  CodeEditor,
  DiffView,
  type CodeEditorHandle,
} from "@/features/editor/CodeEditor";
import {
  EditorDialog,
  ExpandEditorButton,
} from "@/features/editor/EditorDialog";
import { locatePointer } from "@/features/editor/pointerMap";
import { useCapabilities } from "@/features/access/useCapabilities";
import { useFolderList } from "@/features/folders/api";
import { isNotFound, problemOf } from "@/lib/unexpected";
import { firstSemver } from "@/lib/text";
import { contractName } from "./ContractLayout";
import { publishedDocumentStatus } from "./ContractVersions";
import { publishStatusCopy } from "./copy";
import {
  contractKeys,
  fetchDraftDocument,
  fileNameFor,
  formatOf,
  saveText,
  useApi,
  useContract,
} from "./api";
import { FindingsPanel, markersFor } from "./FindingsPanel";
import { replaceTopLevelId, replaceTopLevelStatus } from "./idEdit";
import { documentStatus, PublishDialog } from "./PublishDialog";
import { flattenReport, hasFinding, summaryOf } from "./report";
import styles from "./Contracts.module.css";
import { PrincipalName, usePrincipalLabel } from "@/components/PrincipalName";

type ReportState = {
  kind: "save" | "validate" | "gate";
  report: unknown;
  /** The buffer the report was computed for. */
  text: string;
  revision: number;
  runId?: string | null;
};

export function semverConflictCopy(
  problem: Problem,
  declared: string | null,
): string {
  const ext = problem.kind === "problem" ? (problem.extensions ?? {}) : {};
  const highest =
    typeof ext.highest_semver === "string" ? ext.highest_semver : null;
  return `Version ${declared ?? "this version"} already exists or is below the highest${
    highest ? ` (${highest})` : ""
  }. Choose a higher version.`;
}

export function breakingCopy(message: string, declared: string | null): string {
  const required = firstSemver(message);
  return `Breaking changes need a new major version. Declared ${declared ?? "this version"}${
    required ? `; required at least ${required}` : ""
  }.`;
}

/** The two ids of a 422 odcs-id-mismatch, or null for any other problem. */
export function idMismatchOf(
  problem: Problem | null,
): { expected: string; found: string | null } | null {
  if (problem?.kind !== "problem" || problem.slug !== "odcs-id-mismatch") {
    return null;
  }
  const ext = problem.extensions ?? {};
  if (typeof ext.expected_odcs_id !== "string" || ext.expected_odcs_id === "") {
    return null;
  }
  return {
    expected: ext.expected_odcs_id,
    found: typeof ext.found_odcs_id === "string" ? ext.found_odcs_id : null,
  };
}

function isConflict(error: unknown): boolean {
  return (
    isApiError(error) &&
    error.problem.kind === "problem" &&
    (error.problem.status === 412 ||
      (error.problem.status === 409 &&
        error.problem.slug === "precondition-failed"))
  );
}

export function DraftEditor({
  contractId,
  draftId,
}: {
  contractId: string;
  draftId: string;
}) {
  const api = useApi();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const contract = useContract(contractId);
  const caps = useCapabilities(contract.data?.folder_id ?? null);
  const folders = useFolderList(true).data?.folders ?? [];
  const archived =
    folders.find((folder) => folder.id === contract.data?.folder_id)
      ?.archived ?? false;
  const editorRef = useRef<CodeEditorHandle | null>(null);
  const leaving = useRef(false);

  const meta = useQuery({
    queryKey: contractKeys.draft(contractId, draftId),
    queryFn: async (): Promise<ContractDraft> => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}",
        {
          params: { path: { contract_id: contractId, draft_id: draftId } },
        },
      );
      if (!data) throw new Error("GET draft had no body");
      return data;
    },
    retry: false,
  });
  const doc = useQuery({
    queryKey: [...contractKeys.draft(contractId, draftId), "document"],
    queryFn: () => fetchDraftDocument(api, contractId, draftId),
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  const [buffer, setBuffer] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [revision, setRevision] = useState<number | null>(null);
  const [declared, setDeclared] = useState<string | null>(null);
  const [report, setReport] = useState<ReportState | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<
    "save" | "validate" | "publish" | "keep" | null
  >(null);
  const [conflict, setConflict] = useState<ContractDraft | null>(null);
  // The server's current document for the conflict comparison (null: it could not be loaded).
  const [conflictText, setConflictText] = useState<string | null>(null);
  const conflictBy = usePrincipalLabel(conflict?.updated_by);
  const [publishOpen, setPublishOpen] = useState(false);
  const [publishProblem, setPublishProblem] = useState<Problem | null>(null);
  const [semverProblem, setSemverProblem] = useState<Problem | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  // The full-screen editor; the page editor is unmounted while it is open.
  const [expanded, setExpanded] = useState(false);

  // Seed the buffer once from the first load (and after an explicit reload).
  if (doc.data && meta.data && saved === null) {
    setSaved(doc.data.text);
    setBuffer(doc.data.text);
    setEtag(meta.data.etag);
    setRevision(meta.data.revision);
    setDeclared(meta.data.declared_version);
  }

  const text = buffer ?? "";
  const dirty = saved !== null && buffer !== saved;
  // Publish is disabled while dirty, so the saved bytes are exactly what publish sends.
  const savedStatus = useMemo(() => documentStatus(saved), [saved]);
  const language = formatOf(meta.data?.media_type);
  // "Change status to active" in the publish dialog: only an existing status value is rewritten.
  const statusFixed = useMemo(
    () =>
      saved !== null && savedStatus !== undefined && savedStatus !== "active"
        ? replaceTopLevelStatus(saved, language, "active")
        : null,
    [saved, savedStatus, language],
  );
  const readOnly =
    !caps.isPending && (!caps.can("contract:update") || archived);
  const canPublish = caps.can("contract:publish") && !archived;

  const blocker = useBlocker({
    shouldBlockFn: () => dirty && !leaving.current,
    enableBeforeUnload: () => dirty,
    withResolver: true,
  });

  const findings = useMemo(
    () => (report ? flattenReport(report.report) : []),
    [report],
  );
  const stale = report !== null && report.text !== text;
  // A refused save whose document id is not the contract's: name both ids and offer the fix.
  const idMismatch = idMismatchOf(problem);
  const idFixed = idMismatch
    ? replaceTopLevelId(text, language, idMismatch.expected)
    : null;
  const locate = (pointer: string) =>
    locatePointer(report?.text ?? text, pointer, language);
  const markers = useMemo(
    () =>
      report && !stale
        ? markersFor(findings, (pointer) =>
            locatePointer(report.text, pointer, language),
          )
        : [],
    [findings, report, stale, language],
  );

  async function put(body: string, ifMatch: string) {
    const { data } = await api.PUT(
      "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}",
      {
        params: {
          path: { contract_id: contractId, draft_id: draftId },
          header: { "If-Match": ifMatch },
        },
        body,
        bodySerializer: (value: string) => value,
        headers: {
          "Content-Type": meta.data?.media_type ?? "application/yaml",
        },
      },
    );
    if (!data) throw new Error("PUT draft had no body");
    setSaved(body);
    setEtag(data.draft.etag);
    setRevision(data.draft.revision);
    setDeclared(data.draft.declared_version);
    const validation = data.validation as { status?: string };
    setReport({
      kind: "save",
      report:
        validation.status === "completed"
          ? data.validation
          : { structural: data.structural_validation },
      text: body,
      revision: data.draft.revision,
    });
    queryClient.setQueryData(
      contractKeys.draft(contractId, draftId),
      data.draft,
    );
    void queryClient.invalidateQueries({
      queryKey: contractKeys.drafts(contractId),
    });
    void queryClient.invalidateQueries({
      queryKey: contractKeys.detail(contractId),
    });
    return data;
  }

  async function openConflict() {
    const [{ data }, server] = await Promise.all([
      api.GET("/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}", {
        params: { path: { contract_id: contractId, draft_id: draftId } },
      }),
      // Same notation as the buffer, so the comparison shows real changes only.
      fetchDraftDocument(api, contractId, draftId, language).then(
        (result) => result.text,
        () => null,
      ),
    ]);
    setConflictText(server);
    // Dialogs never nest: a save refused from the full-screen editor closes it first.
    setExpanded(false);
    setConflict(data ?? null);
  }

  async function save(): Promise<boolean> {
    if (etag === null || buffer === null) return false;
    setBusy("save");
    setProblem(null);
    setNotice(null);
    try {
      await put(buffer, etag);
      setNotice("Draft saved.");
      return true;
    } catch (error) {
      if (isConflict(error)) await openConflict();
      else setProblem(problemOf(error));
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function validate() {
    if (dirty && !(await save())) return;
    setBusy("validate");
    setProblem(null);
    try {
      const { data } = await api.POST(
        "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/validate",
        { params: { path: { contract_id: contractId, draft_id: draftId } } },
      );
      if (data)
        setReport({
          kind: "validate",
          report: data.report,
          text: buffer ?? "",
          revision: revision ?? 0,
        });
    } catch (error) {
      setProblem(problemOf(error));
    } finally {
      setBusy(null);
    }
  }

  async function reloadLatest() {
    setConflict(null);
    setReport(null);
    // Re-seed only once the newer server copy is in the cache: clearing first would re-seed
    // from the still-cached old document (and its old ETag) on the next render.
    await queryClient.invalidateQueries({
      queryKey: contractKeys.draft(contractId, draftId),
    });
    setSaved(null);
    setBuffer(null);
  }

  async function keepMine() {
    if (!conflict || buffer === null) return;
    setBusy("keep");
    try {
      await put(buffer, conflict.etag);
      setConflict(null);
      setNotice("Draft saved over the newer server copy.");
    } catch (error) {
      setConflict(null);
      setProblem(problemOf(error));
    } finally {
      setBusy(null);
    }
  }

  async function publish() {
    if (etag === null) return;
    setBusy("publish");
    setPublishProblem(null);
    setSemverProblem(null);
    try {
      const { data } = await api.POST(
        "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/publish",
        {
          params: {
            path: { contract_id: contractId, draft_id: draftId },
            header: { "If-Match": etag },
          },
        },
      );
      setPublishOpen(false);
      // Publishing consumes the draft: leave the editor first, then drop the draft's queries so
      // nothing refetches it (a 404), and refresh the rest of the contract.
      leaving.current = true;
      if (data)
        await navigate({
          to: "/contracts/$contractId/versions",
          params: { contractId },
          search: {
            published: data.semver,
            status: data.status,
            ...publishedDocumentStatus(savedStatus),
          },
        });
      queryClient.removeQueries({
        queryKey: contractKeys.draft(contractId, draftId),
      });
      await queryClient.invalidateQueries({
        queryKey: contractKeys.all(contractId),
      });
      await queryClient.invalidateQueries({ queryKey: contractKeys.lists });
    } catch (error) {
      if (isApiError(error) && error.problem.kind === "problem") {
        const slug = error.problem.slug;
        if (slug === "contract-validation-failed") {
          setPublishOpen(false);
          const ext = error.problem.extensions ?? {};
          setReport({
            kind: "gate",
            report: ext.report,
            text: buffer ?? "",
            revision: revision ?? 0,
            runId: typeof ext.run_id === "string" ? ext.run_id : null,
          });
          return;
        }
        if (slug === "semver-conflict") {
          setPublishOpen(false);
          setSemverProblem(error.problem);
          return;
        }
        if (isConflict(error)) {
          setPublishOpen(false);
          await openConflict();
          return;
        }
      }
      setPublishProblem(problemOf(error));
    } finally {
      setBusy(null);
    }
  }

  if (meta.isError || doc.isError) {
    const error = meta.error ?? doc.error;
    return isNotFound(error) ? (
      <NotFound
        nested
        back={{ label: "drafts", to: `/contracts/${contractId}/drafts` }}
      />
    ) : (
      <ProblemAlert
        problem={problemOf(error)}
        onRetry={() => void meta.refetch()}
      />
    );
  }
  if (!meta.data || buffer === null) return <Skeleton label="draft" rows={8} />;

  const gate = report?.kind === "gate" ? report : null;
  const gateSummary = gate ? summaryOf(gate.report) : null;
  const breaking = gate
    ? hasFinding(gate.report, "semver-breaking-change")
    : undefined;
  const latest = contract.data?.latest_version?.semver ?? null;

  // The editing controls, shared by the page toolbar and the full-screen editor so both render
  // the same handlers, busy states and labels.
  const stateBadge = dirty ? (
    <Badge intent="warning">Unsaved changes</Badge>
  ) : (
    <Badge intent="success">Saved</Badge>
  );
  const saveButton = !readOnly ? (
    <Button
      variant="primary"
      size="small"
      icon={Save}
      busy={busy === "save"}
      busyLabel="Saving…"
      disabled={!dirty}
      onClick={() => void save()}
    >
      Save draft
    </Button>
  ) : null;
  const validateButton = (
    <Button
      variant="secondary"
      size="small"
      icon={CheckCheck}
      busy={busy === "validate"}
      busyLabel="Validating…"
      onClick={() => void validate()}
    >
      {dirty && !readOnly ? "Save and validate" : "Validate draft"}
    </Button>
  );
  const downloadButton = (
    <Button
      variant="ghost"
      size="small"
      icon={Download}
      onClick={() => {
        saveText(
          text,
          fileNameFor(
            contract.data?.odcs_id ?? "contract",
            `draft-${declared ?? "unversioned"}`,
            language,
          ),
          meta.data.media_type,
        );
      }}
    >
      Download buffer
    </Button>
  );
  const statusLine = (
    <p className={styles.status} role="status">
      {notice}
    </p>
  );
  // A refused save: the id mismatch with its fix, or the problem.
  const saveAlerts = idMismatch ? (
    <Alert
      intent="danger"
      title="Draft not saved: the document id does not match this contract"
      action={
        !readOnly && idFixed !== null && idFixed !== text ? (
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              setBuffer(idFixed);
              setProblem(null);
              setNotice(null);
            }}
          >
            Put the contract&apos;s id back
          </Button>
        ) : undefined
      }
    >
      <p>
        This contract&apos;s id is{" "}
        <span className={styles.ident}>{idMismatch.expected}</span>.
        {idMismatch.found !== null ? (
          <>
            {" "}
            The document says{" "}
            <span className={styles.ident}>{idMismatch.found}</span>.
          </>
        ) : null}
      </p>
    </Alert>
  ) : problem ? (
    <ProblemAlert problem={problem} context="action" />
  ) : null;
  const editorProps = {
    value: text,
    onChange: setBuffer,
    language,
    readOnly,
    markers,
    label: `Draft ${declared ?? ""} document`,
    onSave: () => {
      if (!readOnly && dirty) void save();
    },
  };

  return (
    <section className={styles.stack} aria-label="Draft editor">
      <div className={styles.toolbar}>
        <span className={styles.badges}>
          <Badge intent="neutral">Draft {declared ?? ""}</Badge>
          <Badge intent="neutral">
            Revision {revision ?? meta.data.revision}
          </Badge>
          {stateBadge}
        </span>
        <span className={styles.muted}>
          Last saved by <PrincipalName id={meta.data.updated_by} />
        </span>
      </div>
      {readOnly ? (
        <Alert intent="info">
          {archived
            ? "Archived folder: read-only."
            : "You can view this draft but not edit it."}
        </Alert>
      ) : null}
      <div
        className={styles.editorToolbar}
        role="toolbar"
        aria-label="Draft actions"
      >
        {saveButton}
        {validateButton}
        {canPublish ? (
          dirty ? (
            <Tooltip content="Save first">
              <span>
                <Button
                  variant="outline-brand"
                  size="small"
                  icon={Rocket}
                  disabled
                >
                  Publish
                </Button>
              </span>
            </Tooltip>
          ) : (
            <Button
              variant="outline-brand"
              size="small"
              icon={Rocket}
              onClick={() => {
                setPublishProblem(null);
                setPublishOpen(true);
              }}
            >
              Publish
            </Button>
          )
        ) : null}
        {latest ? (
          <ButtonLink
            variant="ghost"
            size="small"
            icon={GitCompare}
            to="/contracts/$contractId/diff"
            params={{ contractId }}
            search={{ from: "latest", to: draftId }}
          >
            Compare with Latest
          </ButtonLink>
        ) : null}
        {downloadButton}
        {!readOnly ? (
          <Button
            variant="ghost"
            size="small"
            icon={Trash2}
            onClick={() => {
              setDiscardOpen(true);
            }}
          >
            Discard draft
          </Button>
        ) : null}
        <span className={styles.spacer} />
        <ExpandEditorButton
          onClick={() => {
            setExpanded(true);
          }}
        />
      </div>
      {statusLine}
      {saveAlerts}
      {semverProblem ? (
        <Alert intent="danger" title="Publish refused: version conflict">
          {semverConflictCopy(semverProblem, declared)}
        </Alert>
      ) : null}
      {gate && gateSummary ? (
        <Alert
          intent="danger"
          title={`Publish blocked: ${String(gateSummary.blockingCount)} blocking finding${gateSummary.blockingCount === 1 ? "" : "s"}`}
          action={
            gate.runId ? (
              <CopyButton
                value={gate.runId}
                label="Copy run ID"
                copiedLabel="Run ID copied"
              />
            ) : undefined
          }
        >
          <p>
            Nothing was published. Fix the blocking findings below, save, and
            publish again.
          </p>
          {gate.runId ? (
            <p>
              Run <span className={styles.ident}>{gate.runId}</span>
            </p>
          ) : null}
        </Alert>
      ) : null}
      {breaking ? (
        <Alert
          intent="warning"
          title="Breaking changes need a new major version"
        >
          <p>{breakingCopy(breaking.message, declared)}</p>
          <Link
            to="/contracts/$contractId/diff"
            params={{ contractId }}
            search={{ from: "latest", to: draftId }}
            className={styles.rowLink}
          >
            View breaking changes
          </Link>
        </Alert>
      ) : null}
      {expanded ? null : (
        // Fills the window while it is the last element; with findings below, a large fixed
        // height keeps them reachable.
        <CodeEditor
          ref={editorRef}
          {...editorProps}
          fit={report ? "tall" : "fill"}
        />
      )}
      {report ? (
        <FindingsPanel
          heading={
            report.kind === "gate"
              ? "Publish gate result"
              : "Validation findings"
          }
          findings={findings}
          locate={stale ? undefined : locate}
          onReveal={(range) => {
            editorRef.current?.revealLine(range.line, range.column);
          }}
          stale={
            stale
              ? {
                  revision: report.revision,
                  onRevalidate: () => void validate(),
                  revalidateLabel:
                    dirty && !readOnly ? "Save and validate" : "Validate draft",
                  busy: busy === "validate" || busy === "save",
                }
              : undefined
          }
        />
      ) : null}

      <EditorDialog
        open={expanded}
        onOpenChange={setExpanded}
        title={`${contract.data ? contractName(contract.data) : "Contract"} · Draft${declared ? ` ${declared}` : ""}`}
        toolbar={
          <>
            {stateBadge}
            {saveButton}
            {validateButton}
            {downloadButton}
          </>
        }
        notices={
          <>
            {statusLine}
            {saveAlerts}
          </>
        }
        editor={editorProps}
      />
      <PublishDialog
        open={publishOpen}
        onOpenChange={setPublishOpen}
        contractId={contractId}
        draftId={draftId}
        declared={declared}
        latest={latest}
        documentStatus={savedStatus}
        busy={busy === "publish"}
        problem={publishProblem}
        onPublish={() => void publish()}
        onChangeStatus={
          !readOnly && statusFixed !== null && statusFixed !== saved
            ? () => {
                setPublishOpen(false);
                setBuffer(statusFixed);
                setNotice(publishStatusCopy.changed);
              }
            : undefined
        }
      />
      <Dialog
        open={conflict !== null}
        size={1280}
        onOpenChange={(open) => {
          if (!open) setConflict(null);
        }}
        title="This draft changed on the server"
        description={
          conflict
            ? `Someone saved revision ${String(conflict.revision)} (by ${conflictBy}) after you opened it. Your edits are not saved yet.`
            : undefined
        }
        footer={
          <>
            <Button
              variant="secondary"
              size="small"
              onClick={() => void reloadLatest()}
            >
              Reload latest
            </Button>
            <Button
              variant="primary"
              size="small"
              busy={busy === "keep"}
              busyLabel="Saving…"
              onClick={() => void keepMine()}
            >
              Keep my edits
            </Button>
          </>
        }
      >
        <div className={styles.stack}>
          <p className={styles.muted}>
            &quot;Reload latest&quot; discards your edits and loads the server
            copy. &quot;Keep my edits&quot; saves your buffer over it.
          </p>
          {conflict && conflictText !== null ? (
            <>
              <div className={styles.diffCaptions} aria-hidden="true">
                <span>Server copy (revision {String(conflict.revision)})</span>
                <span>Your unsaved edits</span>
              </div>
              <DiffView
                original={conflictText}
                modified={text}
                language={language}
                height="24rem"
                originalLabel={`Server copy, revision ${String(conflict.revision)}`}
                modifiedLabel="Your unsaved edits"
              />
            </>
          ) : conflict ? (
            <Alert intent="warning">
              The server copy could not be loaded for comparison. Open the draft
              in another tab to see it before you choose.
            </Alert>
          ) : null}
        </div>
      </Dialog>
      <Dialog
        open={blocker.status === "blocked"}
        onOpenChange={(open) => {
          if (!open) blocker.reset?.();
        }}
        title="Leave without saving?"
        description="Your unsaved changes to this draft will be lost."
        footer={
          <>
            <Button
              variant="secondary"
              size="small"
              onClick={() => blocker.reset?.()}
            >
              Keep editing
            </Button>
            <Button
              variant="danger"
              size="small"
              onClick={() => blocker.proceed?.()}
            >
              Leave without saving
            </Button>
          </>
        }
      />
      <ConfirmDialog
        open={discardOpen}
        onOpenChange={setDiscardOpen}
        title={`Discard draft ${declared ?? ""}?`}
        body="The draft is removed. Published versions are not affected."
        objectName="draft"
        confirmLabel="Discard draft"
        busyLabel="Discarding…"
        onConfirm={async () => {
          await api.DELETE(
            "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}",
            {
              params: {
                path: { contract_id: contractId, draft_id: draftId },
                header: { "If-Match": etag ?? meta.data.etag },
              },
            },
          );
          leaving.current = true;
          await queryClient.invalidateQueries({
            queryKey: contractKeys.drafts(contractId),
          });
          await navigate({
            to: "/contracts/$contractId/drafts",
            params: { contractId },
          });
        }}
      />
    </section>
  );
}
