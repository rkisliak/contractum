// The Versions tab (D-UI-11-10): published versions with their rule keys, enforce date and
// deprecation; "View rules" shows a version's rules read-only in the same form; "Deprecate"
// (owner, ruleset:deprecate) stops new attachments.
import { WhenBy } from "@/components/DataTable/WhenBy";
import { Eye } from "lucide-react";
import { useState } from "react";
import type { RuleSetVersionSummary } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { problemOf } from "@/lib/unexpected";
import { useRuleSetVersion, useRuleSetWrites } from "./api";
import { formatUtcDate } from "./rollout";
import { RuleForm } from "./RuleForm";
import { formFromRule } from "./ruleValues";
import styles from "@/features/governance/gov.module.css";
import { PrincipalName } from "@/components/PrincipalName";

export function RuleSetVersions({
  ruleSetId,
  name,
  versions,
  via,
  canDeprecate,
}: {
  ruleSetId: string;
  name: string;
  versions: RuleSetVersionSummary[];
  /** Folder of the read-only available view, or null for the owner routes. */
  via: string | null;
  canDeprecate: boolean;
}) {
  const writes = useRuleSetWrites(ruleSetId);
  const [viewing, setViewing] = useState<number | null>(null);
  const [deprecating, setDeprecating] = useState<number | null>(null);
  const detail = useRuleSetVersion(ruleSetId, viewing, via);

  const columns: Column<RuleSetVersionSummary>[] = [
    {
      id: "number",
      header: "Version",
      rowHeader: true,
      sortValue: (row) => row.version_no,
      cell: (row) => `Version ${String(row.version_no)}`,
    },
    {
      id: "rules",
      header: "Rules",
      cell: (row) => (
        <span className={styles.badges}>
          {row.rule_keys.map((key) => (
            <Badge key={key} intent="neutral" mono>
              {key}
            </Badge>
          ))}
        </span>
      ),
    },
    {
      id: "enforce",
      header: "Enforce from",
      cell: (row) =>
        row.enforce_at === null
          ? "Not mandatory"
          : `${formatUtcDate(row.enforce_at)} (UTC)`,
    },
    {
      id: "published",
      header: "Published",
      cell: (row) => (
        <WhenBy
          when={formatDateTime(row.published_at)}
          by={<PrincipalName id={row.published_by} />}
        />
      ),
    },
    {
      id: "state",
      header: "State",
      cell: (row) =>
        row.deprecated_at === null ? (
          <Badge intent="success">Active</Badge>
        ) : (
          <Badge intent="warning">Deprecated</Badge>
        ),
    },
    {
      id: "actions",
      header: "Actions",
      cell: (row) => (
        <span className={styles.actions}>
          <Button
            variant="ghost"
            size="small"
            icon={Eye}
            aria-label={`View rules of version ${String(row.version_no)}`}
            onClick={() => {
              setViewing(row.version_no);
            }}
          >
            View rules
          </Button>
          {canDeprecate && row.deprecated_at === null ? (
            <Button
              variant="ghost"
              size="small"
              aria-label={`Deprecate version ${String(row.version_no)}`}
              onClick={() => {
                setDeprecating(row.version_no);
              }}
            >
              Deprecate
            </Button>
          ) : null}
        </span>
      ),
    },
  ];

  return (
    <>
      {versions.length === 0 ? (
        <EmptyState
          level={2}
          heading="No published versions"
          body="Check the draft and publish it to create version 1."
        />
      ) : (
        <DataTable
          caption={`Versions of ${name}`}
          captionHidden
          columns={columns}
          rows={[...versions].sort((a, b) => b.version_no - a.version_no)}
          getRowId={(row) => String(row.version_no)}
        />
      )}
      <Dialog
        open={viewing !== null}
        onOpenChange={(open) => {
          if (!open) setViewing(null);
        }}
        size={1280}
        title={`Version ${String(viewing ?? "")} of ${name}`}
        description="Read-only. Published versions never change."
      >
        {detail.isError ? (
          <ProblemAlert problem={problemOf(detail.error)} />
        ) : detail.data === undefined ? (
          <Skeleton label="version" rows={6} />
        ) : (
          <div className={styles.stack}>
            <p className={styles.muted}>
              Rule language <code>{detail.data.dsl_version}</code>
            </p>
            {detail.data.rules.map((rule) => (
              <section
                key={rule.key}
                className={styles.section}
                aria-label={`Rule ${rule.key}`}
              >
                <h3 className={styles.sectionHeading}>{rule.key}</h3>
                <RuleForm
                  values={formFromRule(rule)}
                  readOnly
                  keyLocked
                  idPrefix={`v${String(detail.data.version_no)}-${rule.key}`}
                />
              </section>
            ))}
          </div>
        )}
      </Dialog>
      <ConfirmDialog
        open={deprecating !== null}
        onOpenChange={(open) => {
          if (!open) setDeprecating(null);
        }}
        title={`Deprecate version ${String(deprecating ?? "")}?`}
        body="It can no longer be newly attached. Contracts that pinned it keep their recorded pins."
        objectName={`version ${String(deprecating ?? "")}`}
        confirmLabel="Deprecate version"
        busyLabel="Deprecating…"
        onConfirm={async () => {
          if (deprecating === null) return;
          await writes.deprecate.mutateAsync(deprecating);
        }}
      />
    </>
  );
}
