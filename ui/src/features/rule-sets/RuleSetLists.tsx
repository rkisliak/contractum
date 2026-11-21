// The two rule-set lists of a folder (D-UI-11-02), shared by /rule-sets and the folder Rule
// sets tab: "Defined here" (the folder's own sets, drafts included) and "Available here
// (inherited)" (published sets from this folder, every folder above and the platform). "Defined
// here" needs `ruleset:read` at the folder (the API hides the list behind a 404 otherwise), so a
// caller who may only read published sets sees only the second list.
import { Link } from "@tanstack/react-router";
import { ListChecks, Plus } from "lucide-react";
import { useState } from "react";
import type { AvailableRuleSet, Folder, RuleSet } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { DataTable, type Column } from "@/components/DataTable";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { folderName } from "@/features/folders/tree";
import { OwningFolder } from "@/features/folders/OwningFolder";
import { problemOf } from "@/lib/unexpected";
import { useAvailableRuleSets, useFolderRuleSets } from "./api";
import { CreateRuleSetDialog } from "./CreateRuleSetDialog";
import { rolloutStatus, scopeLabel } from "./rollout";
import styles from "@/features/governance/gov.module.css";

export function RuleSetLists({ folder }: { folder: Folder }) {
  const caps = useCapabilities(folder.id);
  const [creating, setCreating] = useState(false);
  const canCreate = caps.can("ruleset:create") && !folder.archived;
  const canReadDrafts = caps.can("ruleset:read");
  const name = folderName(folder);
  const createButton = canCreate ? (
    <Button
      variant="primary"
      size="small"
      icon={Plus}
      onClick={() => {
        setCreating(true);
      }}
    >
      Create rule set
    </Button>
  ) : undefined;
  return (
    <div className={styles.stack}>
      {canReadDrafts ? (
        <section className={styles.section} aria-labelledby="rule-sets-here">
          <div className={styles.sectionHeader}>
            <h2 id="rule-sets-here" className={styles.sectionHeading}>
              Defined here
            </h2>
            {createButton}
          </div>
          <DefinedHere folder={folder} canCreate={canCreate} />
        </section>
      ) : null}
      <section className={styles.section} aria-labelledby="rule-sets-available">
        <h2 id="rule-sets-available" className={styles.sectionHeading}>
          Available here (inherited)
        </h2>
        <AvailableHere folder={folder} />
      </section>
      <CreateRuleSetDialog
        open={creating}
        onOpenChange={setCreating}
        folderId={folder.id}
        folderName={name}
      />
    </div>
  );
}

function DefinedHere({
  folder,
  canCreate,
}: {
  folder: Folder;
  canCreate: boolean;
}) {
  const list = useFolderRuleSets(folder.id);
  const columns: Column<RuleSet>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      sortValue: (row) => row.name,
      cell: (row) => (
        <Link
          to="/rule-sets/$ruleSetId"
          params={{ ruleSetId: row.id }}
          className={styles.rowLink}
        >
          {row.name}
        </Link>
      ),
    },
    {
      id: "scope",
      header: "Scope",
      cell: (row) => scopeLabel(row.scope, row.folder_kind),
    },
    {
      id: "published",
      header: "Published versions",
      align: "end",
      sortValue: (row) => row.publish_counter,
      cell: (row) => row.publish_counter,
    },
    {
      id: "revision",
      header: "Draft revision",
      align: "end",
      cell: (row) => row.draft_revision,
    },
    {
      id: "mandatory",
      header: "Mandatory",
      cell: (row) =>
        row.mandatory ? (
          <Badge intent="warning">Mandatory</Badge>
        ) : (
          <Badge intent="neutral">Optional</Badge>
        ),
    },
  ];
  if (list.isError)
    return (
      <ProblemAlert
        problem={problemOf(list.error)}
        onRetry={() => void list.refetch()}
      />
    );
  if (list.data === undefined) return <Skeleton label="rule sets" rows={2} />;
  if (list.data.length === 0)
    return (
      <EmptyState
        level={3}
        icon={ListChecks}
        heading="No rule sets defined here"
        body={
          canCreate
            ? "Create the first rule set, for example a rule that every contract has a description: has(this.description)."
            : "Rule sets created at this folder appear here."
        }
      />
    );
  return (
    <DataTable
      caption={`Rule sets defined in ${folderName(folder)}`}
      captionHidden
      columns={columns}
      rows={list.data}
      getRowId={(row) => row.id}
    />
  );
}

function AvailableHere({ folder }: { folder: Folder }) {
  const list = useAvailableRuleSets(folder.id);
  const columns: Column<AvailableRuleSet>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      sortValue: (row) => row.name,
      cell: (row) => (
        <Link
          to="/rule-sets/$ruleSetId"
          params={{ ruleSetId: row.id }}
          search={{ via: folder.id }}
          className={styles.rowLink}
        >
          {row.name}
        </Link>
      ),
    },
    {
      id: "owner",
      header: "Owning folder",
      cell: (row) => <OwningFolder folderId={row.folder_id} />,
    },
    {
      id: "scope",
      header: "Scope",
      cell: (row) => scopeLabel(row.scope, row.folder_kind),
    },
    {
      id: "latest",
      header: "Latest version",
      align: "end",
      cell: (row) => row.latest_version,
    },
    {
      id: "active",
      header: "Latest active",
      align: "end",
      cell: (row) =>
        row.latest_active_version === null ? (
          <Badge intent="warning">Deprecated</Badge>
        ) : (
          row.latest_active_version
        ),
    },
    {
      id: "mode",
      header: "Mandatory",
      cell: (row) => {
        const status = rolloutStatus(row.mandatory, row.resolved, row.paused);
        return <Badge intent={status.intent}>{status.label}</Badge>;
      },
    },
  ];
  if (list.isError)
    return (
      <ProblemAlert
        problem={problemOf(list.error)}
        onRetry={() => void list.refetch()}
      />
    );
  if (list.data === undefined)
    return <Skeleton label="available rule sets" rows={2} />;
  if (list.data.length === 0)
    return (
      <EmptyState
        level={3}
        heading="No rule sets apply here"
        body="No published rule set from this folder or above applies yet."
      />
    );
  return (
    <DataTable
      caption={`Rule sets available in ${folderName(folder)}`}
      captionHidden
      columns={columns}
      rows={list.data}
      getRowId={(row) => row.id}
    />
  );
}
