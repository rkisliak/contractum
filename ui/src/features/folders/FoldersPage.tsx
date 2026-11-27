// /folders: the primary folder navigator (D-UI-09-01; the sidebar no longer carries a tree).
// Every organization, project and team the caller can see as a full-width tree-table (Name | Kind |
// Subfolders | Contracts | Actions): the kind badge, the subfolder count (from the loaded list) and
// the contract count (one cached list page per visible row) in fixed columns, plus quick actions in
// fixed slots: "Create {child kind}" where the caller may, and open.
// "Create organization" appears for callers who may create folders at ROOT; "Show archived"
// brings archived folders back with a badge; "Show contracts in tree" lists each expanded folder's own
// contracts under it.
import { ArrowRight, FolderPlus, Plus } from "lucide-react";
import { useState } from "react";
import type { Folder } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button, IconButton, IconButtonLink } from "@/components/Button";
import { Checkbox } from "@/components/form";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { Tooltip } from "@/components/Tooltip";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { childKindOf, kindLabel } from "@/features/access/roles";
import { useCapabilities } from "@/features/access/useCapabilities";
import { headBadges } from "@/features/contracts/versionStatus";
import { cx } from "@/lib/cx";
import { problemOf } from "@/lib/unexpected";
import {
  CONTRACT_COUNT_PAGE,
  FOLDER_CAP,
  useContractCount,
  useFolderList,
} from "./api";
import { folderCopy, treeCopy } from "./copy";
import { CreateFolderDialog } from "./CreateFolderDialog";
import { FolderTree, type TreeColumn } from "./FolderTree";
import { folderName, type FolderNode } from "./tree";
import { contractRowLabel, contractVersionSlot } from "./versionSlot";
import styles from "./FolderPages.module.css";

type CreateTarget = { parent: Folder; childKind: string };

export function FoldersPage({
  showArchived,
  onShowArchivedChange,
  showContracts,
  onShowContractsChange,
}: {
  showArchived: boolean;
  onShowArchivedChange: (value: boolean) => void;
  showContracts: boolean;
  onShowContractsChange: (value: boolean) => void;
}) {
  const list = useFolderList(showArchived);
  const root = list.data?.folders.find((folder) => folder.kind === "root");
  const caps = useCapabilities(root?.id ?? null);
  const [creating, setCreating] = useState<CreateTarget | null>(null);
  const canCreate = root !== undefined && caps.can("folder:create");
  const visible =
    list.data?.folders.filter((folder) => folder.kind !== "root") ?? [];

  const createOrganization = () => {
    if (root) setCreating({ parent: root, childKind: "organization" });
  };

  return (
    <>
      <PageHeader
        title="Folders"
        description="Organizations, projects and teams you can see, with their contracts and subfolders. Open a folder to work in it."
        breadcrumbs={[{ label: "Home", to: "/" }, { label: "Folders" }]}
        actions={
          canCreate ? (
            <Button
              variant="primary"
              size="small"
              icon={Plus}
              onClick={createOrganization}
            >
              {folderCopy.createLabel("organization")}
            </Button>
          ) : undefined
        }
      />
      <div className={styles.stack}>
        <Checkbox
          id="folders-show-archived"
          label="Show archived"
          checked={showArchived}
          onChange={(event) => {
            onShowArchivedChange(event.target.checked);
          }}
        />
        <Checkbox
          id="folders-show-contracts"
          label={treeCopy.toggle}
          checked={showContracts}
          onChange={(event) => {
            onShowContractsChange(event.target.checked);
          }}
        />
        {list.isError ? (
          <ProblemAlert
            problem={problemOf(list.error)}
            onRetry={() => void list.refetch()}
          />
        ) : list.data === undefined ? (
          <Skeleton label="folders" rows={5} />
        ) : visible.length === 0 ? (
          <EmptyState
            icon={FolderPlus}
            heading="No folders yet"
            body={
              canCreate
                ? "Create an organization above to start the hierarchy: organizations hold projects, projects hold teams, and contracts can live at any level."
                : "You do not have access to any folder yet. Ask a folder admin to grant you access."
            }
          />
        ) : (
          <>
            {list.data.truncated ? (
              <Alert intent="info">
                Showing the first {FOLDER_CAP} folders. Open a folder to see
                everything inside it.
              </Alert>
            ) : null}
            <FolderTree
              folders={list.data.folders}
              label="All folders"
              showContracts={showContracts}
              columns={FOLDER_COLUMNS}
              contractLabel={contractRowLabel}
              actionsColumn={{ header: "Actions", width: "4.5rem" }}
              renderActions={(node, tabbable) => (
                <FolderRowActions
                  node={node}
                  tabbable={tabbable}
                  onCreate={setCreating}
                />
              )}
            />
          </>
        )}
      </div>
      {creating ? (
        <CreateFolderDialog
          open
          onOpenChange={(open) => {
            if (!open) setCreating(null);
          }}
          parentId={creating.parent.id}
          parentName={folderName(creating.parent)}
          childKind={creating.childKind}
        />
      ) : null}
    </>
  );
}

/** Name | Kind | Subfolders | Contracts, each cell in a fixed column so every row lines up. */
const FOLDER_COLUMNS: TreeColumn[] = [
  {
    id: "kind",
    header: "Kind",
    width: "13rem",
    collapsible: true,
    render: (node) => (
      <Badge intent="neutral">
        {node.folder.kind === "root" ? "Platform" : kindLabel(node.folder.kind)}
      </Badge>
    ),
    // A contract row's lifecycle badge(s): text plus icon, from the one mapping (D-08). Under a
    // narrow tree they move into the name cell, so the state is never lost.
    renderContract: (contract) => (
      <span className={styles.badges}>
        {headBadges(contract).map((badge) => (
          <Badge key={badge.key} intent={badge.intent} icon={badge.icon}>
            {badge.label}
          </Badge>
        ))}
      </span>
    ),
    narrowInline: true,
  },
  {
    id: "subfolders",
    header: "Subfolders",
    width: "6rem",
    align: "end",
    collapsible: true,
    render: (node) =>
      node.folder.kind === "team" ? (
        <>
          <span aria-hidden="true">—</span>
          <VisuallyHidden>None</VisuallyHidden>
        </>
      ) : (
        node.children.length
      ),
  },
  {
    id: "contracts",
    header: "Contracts",
    width: "9rem",
    align: "end",
    collapsible: true,
    render: (node) => <ContractCount node={node} />,
    // A contract row's Latest semver, or a muted word before the first publish (D-10).
    renderContract: (contract) => {
      const slot = contractVersionSlot(contract);
      return (
        <span className={cx(slot.muted && styles.mutedSlot)}>{slot.text}</span>
      );
    },
  },
];

function ContractCount({ node }: { node: FolderNode }) {
  const contracts = useContractCount(node.folder.id);
  if (contracts.data === undefined) return null;
  return (
    <>
      {contracts.data.more
        ? `${String(CONTRACT_COUNT_PAGE)}+`
        : String(contracts.data.count)}
    </>
  );
}

/** Open the folder; create a child folder when the caller may (the API re-checks). */
function FolderRowActions({
  node,
  tabbable,
  onCreate,
}: {
  node: FolderNode;
  tabbable: boolean;
  onCreate: (target: CreateTarget) => void;
}) {
  const { folder } = node;
  const childKind = childKindOf(folder.kind);
  const caps = useCapabilities(
    childKind && !folder.archived ? folder.id : null,
  );
  const name = folderName(folder);
  const canCreate =
    childKind !== null && !folder.archived && caps.can("folder:create");
  const tabIndex = tabbable ? 0 : -1;
  const createLabel =
    childKind === null ? "" : `${folderCopy.createLabel(childKind)} in ${name}`;
  return (
    <>
      {canCreate && childKind ? (
        <Tooltip content={createLabel} side="top">
          <IconButton
            icon={Plus}
            size={24}
            aria-label={createLabel}
            tabIndex={tabIndex}
            onClick={() => {
              onCreate({ parent: folder, childKind });
            }}
          />
        </Tooltip>
      ) : (
        // Keeps "Open" in the same slot on every row.
        <span className={styles.actionSlot} aria-hidden="true" />
      )}
      <Tooltip content={`Open ${name}`} side="top">
        <IconButtonLink
          icon={ArrowRight}
          size={24}
          aria-label={`Open ${name}`}
          tabIndex={tabIndex}
          to="/folders/$folderId"
          params={{ folderId: folder.id }}
        />
      </Tooltip>
    </>
  );
}
