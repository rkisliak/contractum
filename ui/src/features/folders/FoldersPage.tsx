// /folders: the primary folder navigator (D-UI-09-01; the sidebar no longer carries a tree).
// Every organization, project and team the caller can see as a full-width tree-table (Name | Kind |
// Subfolders | Contracts | Actions): the kind badge, the subfolder count (from the loaded list) and
// the contract count (one cached list page per visible row) in fixed columns, plus quick actions in
// fixed slots: "Create {child kind}" where the caller may, and open.
// "Create organization" appears for callers who may create folders at ROOT; "Show archived"
// brings archived folders back with a badge; "Show contracts in tree" lists each expanded folder's own
// contracts under it, each with its lifecycle badge and version after the name and an open action.
// Expand all and Collapse all are disabled while they would change nothing on screen.
import {
  ArrowRight,
  ChevronsDownUp,
  ChevronsUpDown,
  FolderPlus,
  Plus,
} from "lucide-react";
import { useRef, useState } from "react";
import type { ContractSummary, Folder } from "@/api/types";
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
import {
  FolderTree,
  type FolderTreeExpansion,
  type FolderTreeHandle,
  type TreeColumn,
} from "./FolderTree";
import { contractDisplayName, folderName, type FolderNode } from "./tree";
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
  const treeRef = useRef<FolderTreeHandle>(null);
  // Set by the tree before paint; both enabled until it has rendered once.
  const [expansion, setExpansion] = useState<FolderTreeExpansion>({
    canExpandAll: true,
    canCollapseAll: true,
  });
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
        <div className={styles.viewBar}>
          <div
            className={styles.viewOptions}
            role="group"
            aria-label="View options"
          >
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
          </div>
          {visible.length > 0 ? (
            <div
              className={styles.viewActions}
              role="group"
              aria-label={treeCopy.actionsLabel}
            >
              <Button
                variant="outline-neutral"
                size="small"
                icon={ChevronsUpDown}
                disabled={!expansion.canExpandAll}
                onClick={() => treeRef.current?.expandAll()}
              >
                {treeCopy.expandAll}
              </Button>
              <Button
                variant="outline-neutral"
                size="small"
                icon={ChevronsDownUp}
                disabled={!expansion.canCollapseAll}
                onClick={() => treeRef.current?.collapseAll()}
              >
                {treeCopy.collapseAll}
              </Button>
            </div>
          ) : null}
        </div>
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
              ref={treeRef}
              onExpansionChange={setExpansion}
              folders={list.data.folders}
              label="All folders"
              showContracts={showContracts}
              columns={FOLDER_COLUMNS}
              contractLabel={contractRowLabel}
              renderContractMeta={(contract) => (
                <ContractRowMeta contract={contract} />
              )}
              renderContractActions={(contract, tabbable) => (
                <ContractRowActions contract={contract} tabbable={tabbable} />
              )}
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

/**
 * Name | Kind | Subfolders | Contracts, each cell in a fixed column so every row lines up. The
 * columns are sized for folder content only (the widest kind badge, a count), so the Name track
 * keeps room on a 1024px window; a contract row's name cell spans them.
 */
const FOLDER_COLUMNS: TreeColumn[] = [
  {
    id: "kind",
    header: "Kind",
    width: "7rem",
    collapsible: true,
    render: (node) => (
      <Badge intent="neutral">
        {node.folder.kind === "root" ? "Platform" : kindLabel(node.folder.kind)}
      </Badge>
    ),
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
    width: "6rem",
    align: "end",
    collapsible: true,
    render: (node) => <ContractCount node={node} />,
  },
];

/**
 * After a contract's name: its lifecycle badge(s), text plus icon from the one mapping (D-08), then
 * its version (D-10), so the row reads "Orders [Latest] 1.2.0" like the contract page header.
 */
function ContractRowMeta({ contract }: { contract: ContractSummary }) {
  const slot = contractVersionSlot(contract);
  return (
    <>
      {headBadges(contract).map((badge) => (
        <Badge key={badge.key} intent={badge.intent} icon={badge.icon}>
          {badge.label}
        </Badge>
      ))}
      <span className={cx(styles.versionSlot, slot.muted && styles.mutedSlot)}>
        {slot.text}
      </span>
    </>
  );
}

/** Open the contract, in the slot where a folder row has its open action. */
function ContractRowActions({
  contract,
  tabbable,
}: {
  contract: ContractSummary;
  tabbable: boolean;
}) {
  const label = treeCopy.openContract(contractDisplayName(contract));
  return (
    <>
      {/* Keeps "Open" in the same slot as on a folder row. */}
      <span className={styles.actionSlot} aria-hidden="true" />
      <Tooltip content={label} side="top">
        <IconButtonLink
          icon={ArrowRight}
          size={24}
          aria-label={label}
          tabIndex={tabbable ? 0 : -1}
          to="/contracts/$contractId"
          params={{ contractId: contract.id }}
        />
      </Tooltip>
    </>
  );
}

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
