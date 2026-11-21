// Rule sets tab (D-UI-10-15): the contract's attached rule sets (name, version "latest" or a
// number, attached by and at) with Detach, the read-only "Mandatory here" list, and Attach from
// the rule sets available at the contract's folder ("Track latest" or a specific version).
import { WhenBy } from "@/components/DataTable/WhenBy";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Link2, Unlink } from "lucide-react";
import { useState } from "react";
import type { Problem } from "@/api/problem";
import type { ContractRuleSetAttachment } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { SelectField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { problemOf } from "@/lib/unexpected";
import { contractKeys, useApi, useContract } from "./api";
import styles from "./Contracts.module.css";
import { PrincipalName } from "@/components/PrincipalName";

export function ContractRuleSets({ contractId }: { contractId: string }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const contract = useContract(contractId);
  const folderId = contract.data?.folder_id;
  const caps = useCapabilities(folderId ?? null);
  const canEdit = caps.can("contract:update");
  const [attachOpen, setAttachOpen] = useState(false);
  const [detach, setDetach] = useState<ContractRuleSetAttachment | null>(null);
  const [pick, setPick] = useState("");
  const [pickVersion, setPickVersion] = useState("latest");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const attached = useQuery({
    queryKey: contractKeys.ruleSets(contractId),
    queryFn: async () => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}/rule-sets",
        {
          params: { path: { contract_id: contractId } },
        },
      );
      return data ?? { items: [], attachments_revision: 0 };
    },
    retry: false,
  });
  const available = useQuery({
    queryKey: ["available-rule-sets", folderId],
    enabled: Boolean(folderId),
    queryFn: async () => {
      const { data } = await api.GET(
        "/api/v1/validation/folders/{folder_id}/available-rule-sets",
        {
          params: { path: { folder_id: folderId ?? "" } },
        },
      );
      return data?.items ?? [];
    },
    retry: false,
  });
  const mandatory = (available.data ?? []).filter((set) => set.mandatory);
  const attachedIds = new Set(
    (attached.data?.items ?? []).map((item) => item.rule_set_id),
  );
  const attachable = (available.data ?? []).filter(
    (set) =>
      !set.mandatory &&
      set.latest_active_version !== null &&
      !attachedIds.has(set.id),
  );
  const chosen = attachable.find((set) => set.id === pick);

  async function attach() {
    if (!chosen) return;
    setBusy(true);
    setProblem(null);
    try {
      const { data } = await api.PUT(
        "/api/v1/contracts/contracts/{contract_id}/rule-sets/{rule_set_id}",
        {
          params: { path: { contract_id: contractId, rule_set_id: chosen.id } },
          body: {
            version: pickVersion === "latest" ? "latest" : Number(pickVersion),
          },
        },
      );
      await queryClient.invalidateQueries({
        queryKey: contractKeys.ruleSets(contractId),
      });
      setAttachOpen(false);
      setNotice(
        data && data.status === "mandatory"
          ? `${chosen.name} is already mandatory here, so it always applies.`
          : `${chosen.name} is attached.`,
      );
    } catch (error) {
      setProblem(problemOf(error));
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<ContractRuleSetAttachment>[] = [
    {
      id: "name",
      header: "Rule set",
      rowHeader: true,
      cell: (row) => (
        <Link
          to="/rule-sets/$ruleSetId"
          params={{ ruleSetId: row.rule_set_id }}
          className={styles.rowLink}
        >
          {row.rule_set_name}
        </Link>
      ),
    },
    {
      id: "version",
      header: "Version",
      cell: (row) =>
        row.version === "latest" ? (
          <Badge intent="brand">Tracks latest</Badge>
        ) : (
          `Version ${String(row.version)}`
        ),
    },
    {
      id: "attached",
      header: "Attached",
      cell: (row) => (
        <WhenBy
          when={formatDateTime(row.attached_at)}
          by={<PrincipalName id={row.attached_by} />}
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
            icon={Unlink}
            aria-label={`Detach ${row.rule_set_name}`}
            onClick={() => {
              setDetach(row);
            }}
          >
            Detach
          </Button>
        ) : null,
    },
  ];

  return (
    <section className={styles.stack} aria-label="Rule sets">
      <div className={styles.toolbar}>
        <p className={styles.muted}>
          Attached rule sets raise the bar for this contract: their error
          findings block publishing.
        </p>
        <span className={styles.spacer} />
        {canEdit ? (
          <Button
            variant="primary"
            size="small"
            icon={Link2}
            onClick={() => {
              setPick("");
              setPickVersion("latest");
              setProblem(null);
              setAttachOpen(true);
            }}
          >
            Attach rule set
          </Button>
        ) : null}
      </div>
      <p className={styles.status} role="status">
        {notice}
      </p>
      {attached.error ? (
        <ProblemAlert
          problem={problemOf(attached.error)}
          onRetry={() => void attached.refetch()}
        />
      ) : !attached.data ? (
        <Skeleton label="rule sets" rows={3} />
      ) : attached.data.items.length === 0 ? (
        <EmptyState
          heading="No attached rule sets"
          body="Only the mandatory rule sets below apply to this contract."
        />
      ) : (
        <DataTable
          caption="Attached rule sets"
          columns={columns}
          rows={attached.data.items}
          getRowId={(row) => row.rule_set_id}
        />
      )}
      <h2 className={styles.sectionHeading}>Mandatory here</h2>
      {available.error ? (
        <ProblemAlert problem={problemOf(available.error)} />
      ) : !available.data ? (
        <Skeleton label="mandatory rule sets" rows={2} />
      ) : mandatory.length === 0 ? (
        <p className={styles.muted}>
          No mandatory rule sets apply at this folder.
        </p>
      ) : (
        <ul className={styles.changeList}>
          {mandatory.map((set) => (
            <li key={set.id} className={styles.changeItem}>
              <span className={styles.badges}>
                <strong>{set.name}</strong>
                <Badge intent="brand">
                  {set.scope === "platform"
                    ? "Platform"
                    : (set.folder_kind ?? "Folder")}
                </Badge>
                {set.resolved.map((entry) => (
                  <Badge
                    key={entry.version_no}
                    intent={entry.mode === "enforce" ? "danger" : "warning"}
                  >
                    v{entry.version_no}{" "}
                    {entry.paused
                      ? "paused (warns only)"
                      : entry.mode === "enforce"
                        ? "enforced"
                        : `warns until ${entry.enforce_at ? formatDateTime(entry.enforce_at) : "later"}`}
                  </Badge>
                ))}
              </span>
              {set.description ? (
                <span className={styles.muted}>{set.description}</span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      <Dialog
        open={attachOpen}
        onOpenChange={setAttachOpen}
        title="Attach a rule set"
        description="Published rule sets from this folder, its ancestors and the platform."
        footer={
          <>
            <Button
              variant="secondary"
              size="small"
              onClick={() => {
                setAttachOpen(false);
              }}
            >
              Go back
            </Button>
            <Button
              variant="primary"
              size="small"
              busy={busy}
              busyLabel="Attaching…"
              disabled={!chosen}
              onClick={() => void attach()}
            >
              Attach rule set
            </Button>
          </>
        }
      >
        <div className={styles.stackTight}>
          {attachable.length === 0 ? (
            <Alert intent="info">
              Every available rule set is already attached or mandatory here.
            </Alert>
          ) : null}
          <SelectField
            id="attach-rule-set"
            label="Rule set"
            placeholder="Choose a rule set"
            options={attachable.map((set) => ({
              value: set.id,
              label: `${set.name} (${set.scope === "platform" ? "platform" : (set.folder_kind ?? "folder")})`,
            }))}
            value={pick}
            onChange={(event) => {
              setPick(event.target.value);
              setPickVersion("latest");
            }}
          />
          <SelectField
            id="attach-version"
            label="Version"
            disabled={!chosen}
            options={[
              { value: "latest", label: "Track latest" },
              ...(chosen?.versions ?? [])
                .filter((version) => version.deprecated_at === null)
                .map((version) => ({
                  value: String(version.version_no),
                  label: `Version ${String(version.version_no)}`,
                })),
            ]}
            value={pickVersion}
            onChange={(event) => {
              setPickVersion(event.target.value);
            }}
          />
          {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        </div>
      </Dialog>
      <ConfirmDialog
        open={detach !== null}
        onOpenChange={(open) => {
          if (!open) setDetach(null);
        }}
        title={`Detach ${detach?.rule_set_name ?? "rule set"}?`}
        body="Future validations of this contract stop using it. Already-published versions keep their recorded pins."
        objectName={detach?.rule_set_name ?? "rule set"}
        confirmLabel={`Detach ${detach?.rule_set_name ?? "rule set"}`}
        busyLabel="Detaching…"
        onConfirm={async () => {
          if (!detach) return;
          await api.DELETE(
            "/api/v1/contracts/contracts/{contract_id}/rule-sets/{rule_set_id}",
            {
              params: {
                path: {
                  contract_id: contractId,
                  rule_set_id: detach.rule_set_id,
                },
              },
            },
          );
          await queryClient.invalidateQueries({
            queryKey: contractKeys.ruleSets(contractId),
          });
          setNotice(`${detach.rule_set_name} is detached.`);
        }}
      />
    </section>
  );
}
