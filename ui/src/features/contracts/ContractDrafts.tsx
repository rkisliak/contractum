// Drafts tab (D-UI-09-11): the contract's parallel drafts with declared version, revision, base
// version and last update; "Open" goes to the editor, "Discard" asks first and sends DELETE with
// If-Match. "New draft" starts from a published version or an existing draft with a bump choice.
import { WhenBy } from "@/components/DataTable/WhenBy";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { FilePlus2, FileText, Trash2 } from "lucide-react";
import { useState } from "react";
import type { Problem } from "@/api/problem";
import type { ContractDraft } from "@/api/types";
import { Button } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, orNotSet, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { SelectField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { problemOf } from "@/lib/unexpected";
import {
  contractKeys,
  useApi,
  useContract,
  useContractVersions,
  useDrafts,
} from "./api";
import styles from "./Contracts.module.css";
import { PrincipalName } from "@/components/PrincipalName";

const BUMPS = [
  { value: "minor", label: "Minor (default)" },
  { value: "major", label: "Major" },
  { value: "patch", label: "Patch" },
  { value: "none", label: "Keep the version" },
];

export function NewDraftDialog({
  contractId,
  open,
  onOpenChange,
  initialSource,
}: {
  contractId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialSource?: string;
}) {
  const api = useApi();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const versions = useContractVersions(contractId, open);
  const drafts = useDrafts(contractId, open);
  const [source, setSource] = useState(initialSource ?? "");
  const [bump, setBump] = useState("minor");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const options = [
    ...(versions.data ?? []).map((version) => ({
      value: `v:${version.semver}`,
      label: `Version ${version.semver}`,
    })),
    ...(drafts.data ?? []).map((draft) => ({
      value: `d:${draft.id}`,
      label: `Draft ${draft.declared_version ?? ""} (revision ${String(draft.revision)})`,
    })),
  ];
  const chosen = source || options[0]?.value || "";

  async function create() {
    setBusy(true);
    setProblem(null);
    try {
      const kind = chosen.slice(0, 1);
      const ref = chosen.slice(2);
      const { data } = await api.POST(
        "/api/v1/contracts/contracts/{contract_id}/drafts",
        {
          params: { path: { contract_id: contractId } },
          body: {
            bump: bump as "major" | "minor" | "patch" | "none",
            ...(kind === "d"
              ? { from_draft_id: ref }
              : kind === "v"
                ? { from_version: ref }
                : {}),
          },
        },
      );
      await queryClient.invalidateQueries({
        queryKey: contractKeys.drafts(contractId),
      });
      onOpenChange(false);
      if (data)
        await navigate({
          to: "/contracts/$contractId/drafts/$draftId",
          params: { contractId, draftId: data.draft.id },
        });
    } catch (error) {
      setProblem(problemOf(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Create a draft"
      description="A new draft starts from a published version or another draft. Publishing it later creates a new version."
      footer={
        <>
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              onOpenChange(false);
            }}
          >
            Go back
          </Button>
          <Button
            variant="primary"
            size="small"
            busy={busy}
            busyLabel="Creating…"
            disabled={options.length === 0}
            onClick={() => void create()}
          >
            Create draft
          </Button>
        </>
      }
    >
      <div className={styles.stackTight}>
        <SelectField
          id="new-draft-source"
          label="Start from"
          options={options}
          value={chosen}
          onChange={(event) => {
            setSource(event.target.value);
          }}
        />
        <SelectField
          id="new-draft-bump"
          label="Version bump"
          helper="Raises the document's version from the source."
          options={BUMPS}
          value={bump}
          onChange={(event) => {
            setBump(event.target.value);
          }}
        />
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
      </div>
    </Dialog>
  );
}

export function ContractDrafts({ contractId }: { contractId: string }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const contract = useContract(contractId);
  const caps = useCapabilities(contract.data?.folder_id ?? null);
  const drafts = useDrafts(contractId);
  const versions = useContractVersions(contractId);
  const [creating, setCreating] = useState(false);
  const [discard, setDiscard] = useState<ContractDraft | null>(null);
  const semverOf = new Map(
    (versions.data ?? []).map((version) => [
      version.version_id,
      version.semver,
    ]),
  );
  const canEdit = caps.can("contract:update");

  const columns: Column<ContractDraft>[] = [
    {
      id: "version",
      header: "Declared version",
      rowHeader: true,
      cell: (row) => (
        <Link
          to="/contracts/$contractId/drafts/$draftId"
          params={{ contractId, draftId: row.id }}
          className={styles.rowLink}
        >
          {row.declared_version ?? "No version"}
        </Link>
      ),
    },
    {
      id: "revision",
      header: "Revision",
      align: "end",
      cell: (row) => row.revision,
    },
    {
      id: "base",
      header: "Based on",
      cell: (row) =>
        row.base_version_id
          ? (semverOf.get(row.base_version_id) ?? "A version")
          : orNotSet(null),
    },
    {
      id: "format",
      header: "Format",
      cell: (row) => (row.media_type.includes("json") ? "JSON" : "YAML"),
    },
    {
      id: "updated",
      header: "Updated",
      sortValue: (row) => row.updated_at,
      cell: (row) => (
        <WhenBy
          when={formatDateTime(row.updated_at)}
          by={<PrincipalName id={row.updated_by} />}
        />
      ),
    },
    {
      id: "actions",
      header: "Actions",
      cell: (row) =>
        canEdit ? (
          <Button
            variant="ghost"
            size="small"
            icon={Trash2}
            aria-label={`Discard draft ${row.declared_version ?? row.id}`}
            onClick={() => {
              setDiscard(row);
            }}
          >
            Discard
          </Button>
        ) : null,
    },
  ];

  return (
    <section className={styles.stack} aria-label="Drafts">
      <div className={styles.toolbar}>
        <p className={styles.muted}>
          Drafts are private working copies. Publish one to create a version.
        </p>
        <span className={styles.spacer} />
        {canEdit ? (
          <Button
            variant="primary"
            size="small"
            icon={FilePlus2}
            onClick={() => {
              setCreating(true);
            }}
          >
            New draft
          </Button>
        ) : null}
      </div>
      {drafts.error ? (
        <ProblemAlert
          problem={problemOf(drafts.error)}
          onRetry={() => void drafts.refetch()}
        />
      ) : drafts.data === undefined ? (
        <Skeleton label="drafts" rows={3} />
      ) : drafts.data.length === 0 ? (
        <EmptyState
          icon={FileText}
          heading="No drafts"
          body="Every draft has been published. Start a new draft from a version to make changes."
        />
      ) : (
        <DataTable
          caption="Drafts"
          captionHidden
          columns={columns}
          rows={drafts.data}
          getRowId={(row) => row.id}
        />
      )}
      {creating ? (
        <NewDraftDialog
          contractId={contractId}
          open={creating}
          onOpenChange={setCreating}
        />
      ) : null}
      <ConfirmDialog
        open={discard !== null}
        onOpenChange={(open) => {
          if (!open) setDiscard(null);
        }}
        title={`Discard draft ${discard?.declared_version ?? ""}?`}
        body="The draft and its unsaved history are removed. Published versions are not affected."
        objectName="draft"
        confirmLabel="Discard draft"
        busyLabel="Discarding…"
        onConfirm={async () => {
          if (!discard) return;
          await api.DELETE(
            "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}",
            {
              params: {
                path: { contract_id: contractId, draft_id: discard.id },
                header: { "If-Match": discard.etag },
              },
            },
          );
          await queryClient.invalidateQueries({
            queryKey: contractKeys.drafts(contractId),
          });
        }}
      />
    </section>
  );
}
