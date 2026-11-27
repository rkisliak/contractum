// The folder tree-table (D-UI-09-02): the WAI-ARIA treegrid pattern (row focus) with a roving
// tabindex. Up and Down move through visible rows, Right expands or enters, Left collapses or goes to the parent, Home and
// End jump, Enter opens the folder, printable keys jump by name (type-ahead). A mouse click on a row
// expands or collapses it; the row's open action (the arrow) opens it. The current folder
// carries aria-current="page" and its ancestors are expanded.
// With `showContracts` every expanded folder also lists its own contracts (one list call per
// expanded folder) as treegrid rows below its subfolders; each is a real link to the contract.
import { Link, useNavigate } from "@tanstack/react-router";
import { ChevronRight, FileText, Folder as FolderIcon } from "lucide-react";
import {
  useId,
  useMemo,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from "react";
import type { ContractSummary, Folder } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Identifier } from "@/components/Identifier";
import { Skeleton } from "@/components/states";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { cx } from "@/lib/cx";
import { useFolderContractQueries } from "./api";
import { treeCopy } from "./copy";
import {
  ancestorIds,
  buildFolderTree,
  contractDisplayName,
  folderName,
  visibleNodes,
  visibleRows,
  type FolderNode,
  type TreeRow,
} from "./tree";
import styles from "./FolderTree.module.css";

export type TreeColumn = {
  id: string;
  header: string;
  /** A CSS track size, fixed so every row lines up ("7rem"). */
  width: string;
  align?: "start" | "end";
  /** Dropped when the tree is narrow (counts give way so the name keeps room). */
  collapsible?: boolean;
  render: (node: FolderNode) => ReactNode;
  /** The cell of a contract row (with `showContracts`); empty when absent. */
  renderContract?: (contract: ContractSummary) => ReactNode;
  /**
   * Under a narrow tree this collapsible column's content moves into the name cell of a contract
   * row, so the state it carries (the lifecycle badge) is never lost.
   */
  narrowInline?: boolean;
};

const NO_FOLDERS: readonly string[] = [];

export type FolderTreeProps = {
  folders: Folder[];
  currentId?: string;
  label?: string;
  /** Called after a folder is opened (the mobile drawer closes itself). */
  onOpen?: () => void;
  /**
   * Tree-table columns after the name (kind, counts), each in a fixed-width column under a header
   * row. Rendered only for visible rows, so per-row queries run only for what is on screen.
   */
  columns?: TreeColumn[];
  /** Header and width of the row-actions column (with `renderActions`). */
  actionsColumn?: { header: string; width: string };
  /**
   * Row quick actions (buttons or links). `tabbable` is true for the row holding the tree's tab
   * stop, so Tab moves from the focused row into its actions and arrows keep moving rows.
   */
  renderActions?: (node: FolderNode, tabbable: boolean) => ReactNode;
  /**
   * Lists each expanded folder's own contracts under its subfolders. Every folder is then
   * expandable (its contracts are unknown until its list call returns). Off by default: the tree
   * then makes no contract request and renders only folders.
   */
  showContracts?: boolean;
  /** The accessible name of a contract row; defaults to its display name. */
  contractLabel?: (contract: ContractSummary) => string;
};

export function FolderTree({
  folders,
  currentId,
  label = "Folders",
  onOpen,
  columns,
  actionsColumn,
  renderActions,
  showContracts = false,
  contractLabel,
}: FolderTreeProps) {
  const navigate = useNavigate();
  const treeId = useId();
  const roots = useMemo(() => buildFolderTree(folders), [folders]);
  // ROOT starts open, so the organizations are visible under it.
  const [expanded, setExpanded] = useState<Set<string>>(
    () =>
      new Set([
        ...folders.filter((f) => f.kind === "root").map((f) => f.id),
        ...(currentId ? ancestorIds(folders, currentId) : []),
      ]),
  );
  const [focusedId, setFocusedId] = useState<string | null>(null);

  // Keep the current folder reachable: when it changes, expand its ancestors once
  // (state adjusted during render, React's documented pattern, not an effect).
  const [revealed, setRevealed] = useState(currentId);
  if (currentId !== revealed) {
    setRevealed(currentId);
    if (currentId) {
      const missing = ancestorIds(folders, currentId).filter(
        (id) => !expanded.has(id),
      );
      if (missing.length > 0) setExpanded(new Set([...expanded, ...missing]));
    }
  }

  const visible = useMemo(
    () => visibleNodes(roots, expanded),
    [roots, expanded],
  );
  // The expanded folders on screen are the ones whose contracts are fetched (D-03): none when the
  // toggle is off, so the tree then makes no list call at all.
  const openFolderIds = useMemo(
    () =>
      showContracts
        ? visible
            .filter((node) => expanded.has(node.folder.id))
            .map((node) => node.folder.id)
        : NO_FOLDERS,
    [showContracts, visible, expanded],
  );
  const folderContracts = useFolderContractQueries(openFolderIds);
  const rows = useMemo(
    () => visibleRows(roots, expanded, showContracts ? folderContracts : null),
    [roots, expanded, showContracts, folderContracts],
  );
  // aria-setsize and aria-posinset of a folder row count its sibling folders only.
  const siblings = useMemo(() => {
    const info = new Map<string, { position: number; size: number }>();
    const walk = (list: FolderNode[]) => {
      list.forEach((node, index) => {
        info.set(node.folder.id, { position: index + 1, size: list.length });
        walk(node.children);
      });
    };
    walk(roots);
    return info;
  }, [roots]);
  const tabStop =
    visible.find((node) => node.folder.id === focusedId)?.folder.id ??
    visible.find((node) => node.folder.id === currentId)?.folder.id ??
    visible[0]?.folder.id;

  const domId = (folderId: string) => `${treeId}-${folderId}`;

  function focusItem(folderId: string) {
    setFocusedId(folderId);
    document.getElementById(domId(folderId))?.focus();
  }

  function toggle(folderId: string, open?: boolean) {
    setExpanded((current) => {
      const next = new Set(current);
      const willOpen = open ?? !next.has(folderId);
      if (willOpen) next.add(folderId);
      else next.delete(folderId);
      return next;
    });
  }

  function openFolder(folderId: string) {
    setFocusedId(folderId);
    void navigate({ to: "/folders/$folderId", params: { folderId } });
    onOpen?.();
  }

  function onKeyDown(event: KeyboardEvent<HTMLElement>, node: FolderNode) {
    // Keys pressed on a row action belong to that action.
    if (event.target !== event.currentTarget) return;
    const id = node.folder.id;
    const index = visible.findIndex((candidate) => candidate.folder.id === id);
    const isOpen = expanded.has(id);
    const hasChildren = node.children.length > 0;
    const expandable = showContracts || hasChildren;
    let handled = true;
    switch (event.key) {
      case "ArrowDown": {
        const next = visible[index + 1];
        if (next) focusItem(next.folder.id);
        break;
      }
      case "ArrowUp": {
        const previous = visible[index - 1];
        if (previous) focusItem(previous.folder.id);
        break;
      }
      case "ArrowRight":
        if (expandable && !isOpen) toggle(id, true);
        else if (hasChildren && node.children[0])
          focusItem(node.children[0].folder.id);
        break;
      case "ArrowLeft":
        if (expandable && isOpen) toggle(id, false);
        else if (node.folder.parent_id && expandedParent(node, visible))
          focusItem(node.folder.parent_id);
        break;
      case "Home":
        if (visible[0]) focusItem(visible[0].folder.id);
        break;
      case "End": {
        const last = visible.at(-1);
        if (last) focusItem(last.folder.id);
        break;
      }
      case "Enter":
      case " ":
        openFolder(id);
        break;
      default:
        if (event.key.length === 1 && /\S/.test(event.key)) {
          const char = event.key.toLowerCase();
          const ordered = [
            ...visible.slice(index + 1),
            ...visible.slice(0, index + 1),
          ];
          const match = ordered.find((candidate) =>
            folderName(candidate.folder).toLowerCase().startsWith(char),
          );
          if (match) focusItem(match.folder.id);
        } else {
          handled = false;
        }
    }
    if (handled) {
      event.preventDefault();
      event.stopPropagation();
    }
  }

  function onClick(event: MouseEvent<HTMLElement>, node: FolderNode) {
    event.stopPropagation();
    const target = event.target as HTMLElement;
    if (target.closest("[data-tree-action]")) return;
    // A click anywhere on the row (name, icon, chevron, facts) expands or collapses it; the row's
    // open action (the arrow) and the keyboard (Enter, Space) open the folder.
    setFocusedId(node.folder.id);
    if (showContracts || node.children.length > 0) toggle(node.folder.id);
  }

  // The APG treegrid: a flat list of rows carrying aria-level/setsize/posinset, each with a name
  // cell, the fact cells and an actions cell. Row actions are real buttons inside a gridcell (a
  // tree may hold only treeitems, so nested buttons there broke aria-required-children). Each row
  // is named by its folder name only (aria-labelledby).
  const cols = columns ?? [];
  const hasActions = renderActions !== undefined;
  const indent = (depth: number): CSSProperties => ({
    paddingInlineStart: `calc(${String(depth)} * var(--spacing-3))`,
  });

  function renderFolderRow(row: Extract<TreeRow, { kind: "folder" }>) {
    const { node } = row;
    const { folder } = node;
    const hasChildren = node.children.length > 0;
    const expandable = showContracts || hasChildren;
    const isOpen = expanded.has(folder.id);
    const current = folder.id === currentId;
    const sibling = siblings.get(folder.id);
    return (
      <div
        key={row.key}
        id={domId(folder.id)}
        role="row"
        aria-level={node.depth + 1}
        aria-setsize={sibling?.size}
        aria-posinset={sibling?.position}
        aria-expanded={expandable ? isOpen : undefined}
        aria-selected={current}
        aria-current={current ? "page" : undefined}
        aria-labelledby={`${domId(folder.id)}-label`}
        tabIndex={folder.id === tabStop ? 0 : -1}
        className={cx(styles.gridRow, styles.item, current && styles.current)}
        onKeyDown={(event) => {
          onKeyDown(event, node);
        }}
        onClick={(event) => {
          onClick(event, node);
        }}
        onFocus={(event) => {
          if (event.target === event.currentTarget) setFocusedId(folder.id);
        }}
      >
        <span role="gridcell" className={styles.row} style={indent(node.depth)}>
          <span
            className={cx(styles.toggle, !expandable && styles.leaf)}
            data-tree-toggle={expandable ? "" : undefined}
            aria-hidden="true"
          >
            {expandable ? (
              <ChevronRight
                size={12}
                strokeWidth={2}
                className={cx(styles.chevron, isOpen && styles.open)}
              />
            ) : null}
          </span>
          <FolderIcon
            size={14}
            strokeWidth={2}
            aria-hidden="true"
            className={styles.folderIcon}
          />
          <span
            id={`${domId(folder.id)}-label`}
            className={styles.label}
            title={folderName(folder)}
          >
            {folder.archived ? (
              // The clip pattern is position: absolute, which makes the hidden span a block, and
              // the accessible name puts a space around a block ("Old , archived"). The hidden
              // span therefore carries the whole name and the visible one is hidden from it.
              <>
                <span aria-hidden="true">{folderName(folder)}</span>
                <VisuallyHidden>{`${folderName(folder)}, archived`}</VisuallyHidden>
              </>
            ) : (
              folderName(folder)
            )}
          </span>
          {folder.archived ? (
            <span className={styles.badge} aria-hidden="true">
              <Badge intent="neutral">Archived</Badge>
            </span>
          ) : null}
        </span>
        {cols.map((column) => (
          <span
            key={column.id}
            role="gridcell"
            className={cx(styles.cell, column.align === "end" && styles.end)}
            data-collapsible={column.collapsible ? "" : undefined}
          >
            {column.render(node)}
          </span>
        ))}
        {hasActions ? (
          <span
            role="gridcell"
            className={cx(styles.cell, styles.end, styles.actions)}
            data-tree-action=""
          >
            {renderActions(node, folder.id === tabStop)}
          </span>
        ) : null}
      </div>
    );
  }

  // A contract is a treegrid row one level below its folder (the treegrid's "treeitem"): its name
  // is a real link, so click, middle-click and the context menu all open the contract.
  function renderContractRow(row: Extract<TreeRow, { kind: "contract" }>) {
    const { contract } = row;
    const rowId = `${treeId}-${row.key}`;
    const displayName = contractDisplayName(contract);
    const named = contract.name !== null && contract.name !== "";
    return (
      <div
        key={row.key}
        id={rowId}
        role="row"
        aria-level={row.depth + 1}
        aria-setsize={row.setSize}
        aria-posinset={row.position}
        aria-labelledby={`${rowId}-label`}
        tabIndex={-1}
        className={cx(styles.gridRow, styles.contractRow)}
      >
        <span role="gridcell" className={styles.row} style={indent(row.depth)}>
          <span className={cx(styles.toggle, styles.leaf)} aria-hidden="true" />
          <FileText
            size={14}
            strokeWidth={2}
            aria-hidden="true"
            className={styles.folderIcon}
          />
          <VisuallyHidden id={`${rowId}-label`}>
            {contractLabel?.(contract) ?? displayName}
          </VisuallyHidden>
          <span className={styles.label} title={displayName}>
            <Link
              to="/contracts/$contractId"
              params={{ contractId: contract.id }}
              tabIndex={-1}
              className={cx(styles.contractLink, !named && styles.muted)}
            >
              {named ? displayName : <Identifier value={displayName} />}
            </Link>
          </span>
        </span>
        {cols.map((column) => (
          <span
            key={column.id}
            role="gridcell"
            className={cx(styles.cell, column.align === "end" && styles.end)}
            data-collapsible={column.collapsible ? "" : undefined}
          >
            {column.renderContract?.(contract) ?? null}
          </span>
        ))}
        {hasActions ? (
          <span
            role="gridcell"
            className={cx(styles.cell, styles.end, styles.actions)}
          />
        ) : null}
      </div>
    );
  }

  // Loading, empty, error and overflow rows trail the contracts of one folder. Loading and empty
  // are not focusable; error and overflow are plain text for now.
  function renderStateRow(
    row: Exclude<TreeRow, { kind: "folder" | "contract" }>,
  ) {
    return (
      <div
        key={row.key}
        role="row"
        aria-level={row.depth + 1}
        className={cx(styles.gridRow, styles.stateRow)}
      >
        <span role="gridcell" className={styles.row} style={indent(row.depth)}>
          <span className={cx(styles.toggle, styles.leaf)} aria-hidden="true" />
          {row.kind === "loading" ? (
            <Skeleton label="contracts" />
          ) : (
            <span className={styles.muted}>
              {row.kind === "empty"
                ? treeCopy.empty
                : row.kind === "error"
                  ? treeCopy.error
                  : treeCopy.overflow}
            </span>
          )}
        </span>
      </div>
    );
  }

  function renderRow(row: TreeRow): ReactNode {
    switch (row.kind) {
      case "folder":
        return renderFolderRow(row);
      case "contract":
        return renderContractRow(row);
      default:
        return renderStateRow(row);
    }
  }

  if (roots.length === 0) return null;
  const tracks = (narrow: boolean) =>
    [
      "minmax(0, 1fr)",
      ...cols
        .filter((column) => !(narrow && column.collapsible))
        .map((column) => column.width),
      hasActions ? (actionsColumn?.width ?? "4.5rem") : "",
    ]
      .filter(Boolean)
      .join(" ");
  return (
    <div
      role="treegrid"
      aria-label={label}
      className={styles.treeTable}
      data-primary-block=""
      style={
        {
          "--tree-cols": tracks(false),
          "--tree-cols-narrow": tracks(true),
        } as CSSProperties
      }
    >
      <div role="row" className={cx(styles.gridRow, styles.header)}>
        <span role="columnheader" className={styles.headerName}>
          Name
        </span>
        {cols.map((column) => (
          <span
            key={column.id}
            role="columnheader"
            className={cx(styles.cell, column.align === "end" && styles.end)}
            data-collapsible={column.collapsible ? "" : undefined}
          >
            {column.header}
          </span>
        ))}
        {hasActions ? (
          <span role="columnheader" className={cx(styles.cell, styles.end)}>
            {actionsColumn?.header ?? "Actions"}
          </span>
        ) : null}
      </div>
      {rows.map(renderRow)}
    </div>
  );
}

function expandedParent(node: FolderNode, visible: FolderNode[]): boolean {
  return visible.some(
    (candidate) => candidate.folder.id === node.folder.parent_id,
  );
}
