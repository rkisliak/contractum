// The folder tree-table (D-UI-09-02): the WAI-ARIA treegrid pattern (row focus) with a roving
// tabindex over every focusable row (folder, contract, overflow and error rows; loading and empty
// rows are skipped). Up and Down move through them, Right expands a closed folder or enters an open
// one, Left collapses an open folder or goes to the parent folder row, Home and End jump, Enter or
// Space opens the row (a folder, a contract, the overflow list) or retries an error row, printable
// keys jump by name (type-ahead). A mouse click on a folder row expands or collapses it; its open
// action (the arrow) opens it. The current folder carries aria-current="page" and its ancestors
// are expanded.
// With `showContracts` every expanded folder also lists its own contracts (one list call per
// expanded folder) as treegrid rows below its subfolders; each is a real link to the contract,
// with the page's meta (lifecycle badge and version) after its name and its own row actions.
// The page learns through `onExpansionChange` whether Expand all and Collapse all would change
// what is on screen, so it can disable them when they would not.
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import {
  AlertTriangle,
  ArrowRight,
  ChevronRight,
  FileText,
  Folder as FolderIcon,
} from "lucide-react";
import {
  useId,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useState,
  type CSSProperties,
  type FocusEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type Ref,
} from "react";
import type { ContractSummary, Folder } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
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
  firstChildKey,
  folderName,
  isExpandableFolder,
  isFocusableRow,
  parentFolderKey,
  typeAheadKey,
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
  /** The cell of a folder row; the cell of a contract row is empty. */
  render: (node: FolderNode) => ReactNode;
};

const NO_FOLDERS: readonly string[] = [];

/** Commands a page can send to its tree (the Folders page's Expand all and Collapse all). */
export type FolderTreeHandle = {
  /** Opens every folder that can open (with contracts shown, every folder). */
  expandAll: () => void;
  /** Closes every folder except Root, so the organizations stay in view. */
  collapseAll: () => void;
};

/** Whether each tree command would change what is on screen (false: it would do nothing). */
export type FolderTreeExpansion = {
  canExpandAll: boolean;
  canCollapseAll: boolean;
};

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
  /** A contract row's quick actions, in the same column and with the same `tabbable` rule. */
  renderContractActions?: (
    contract: ContractSummary,
    tabbable: boolean,
  ) => ReactNode;
  /**
   * Shown right after a contract's name at every width (its lifecycle badge and version); on a
   * narrow tree it wraps onto a second line under the name.
   */
  renderContractMeta?: (contract: ContractSummary) => ReactNode;
  /**
   * Lists each expanded folder's own contracts under its subfolders. Every folder is then
   * expandable (its contracts are unknown until its list call returns). Off by default: the tree
   * then makes no contract request and renders only folders.
   */
  showContracts?: boolean;
  /** The accessible name of a contract row; defaults to its display name. */
  contractLabel?: (contract: ContractSummary) => string;
  /** Receives the Expand all and Collapse all commands. */
  ref?: Ref<FolderTreeHandle>;
  /** Told, before paint, whenever Expand all or Collapse all starts or stops changing anything. */
  onExpansionChange?: (expansion: FolderTreeExpansion) => void;
};

export function FolderTree({
  folders,
  currentId,
  label = "Folders",
  onOpen,
  columns,
  actionsColumn,
  renderActions,
  renderContractActions,
  renderContractMeta,
  showContracts = false,
  contractLabel,
  ref,
  onExpansionChange,
}: FolderTreeProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
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
  // The row last focused, with the folder row that owns it (itself for a folder row), so the tab
  // stop can fall back to that folder when the row leaves the screen.
  const [focused, setFocused] = useState<{
    key: string;
    ownerKey: string;
  } | null>(null);

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
  const contractsShown = showContracts ? folderContracts : null;
  const rows = useMemo(
    () => visibleRows(roots, expanded, contractsShown),
    [roots, expanded, contractsShown],
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
  const rowKeys = useMemo(() => new Set(rows.map((row) => row.key)), [rows]);
  const focusableRows = useMemo(() => rows.filter(isFocusableRow), [rows]);
  // One tab stop for the whole tree: the focused row when it is still on screen, else the folder
  // row that owned it, else the current folder's row, else the first row.
  const tabStopKey =
    (focused && rowKeys.has(focused.key) ? focused.key : undefined) ??
    (focused && rowKeys.has(focused.ownerKey) ? focused.ownerKey : undefined) ??
    (currentId && rowKeys.has(`f:${currentId}`)
      ? `f:${currentId}`
      : undefined) ??
    rows[0]?.key;

  // Ids are built from the row key with every character outside [A-Za-z0-9_-] replaced, so the
  // f: and c: prefixes of a folder and a contract can never collide.
  const domId = (key: string) =>
    `${treeId}-${key.replace(/[^A-Za-z0-9_-]/g, "_")}`;

  function rowFocusProps(row: TreeRow) {
    const ownerKey = row.kind === "folder" ? row.key : `f:${row.folderId}`;
    return {
      id: domId(row.key),
      onFocus: (event: FocusEvent<HTMLElement>) => {
        if (event.target === event.currentTarget)
          setFocused({ key: row.key, ownerKey });
      },
    };
  }

  function focusItem(key: string) {
    const row = rows.find((candidate) => candidate.key === key);
    if (row === undefined) return;
    setFocused({
      key,
      ownerKey: row.kind === "folder" ? row.key : `f:${row.folderId}`,
    });
    document.getElementById(domId(key))?.focus();
  }

  // What Expand all and Collapse all would change, judged on the rows on screen: a hidden folder
  // only matters through a visible ancestor, which then differs itself. A folder without an arrow
  // (no subfolders, and with contracts shown an empty loaded list) changes nothing either way.
  const rootIds = useMemo(
    () => new Set(folders.filter((f) => f.kind === "root").map((f) => f.id)),
    [folders],
  );
  let canExpandAll = false;
  let canCollapseAll = false;
  for (const node of visible) {
    if (!isExpandableFolder(node, contractsShown)) continue;
    const isOpen = expanded.has(node.folder.id);
    if (!isOpen) canExpandAll = true;
    if (isOpen !== rootIds.has(node.folder.id)) canCollapseAll = true;
  }
  useLayoutEffect(() => {
    onExpansionChange?.({ canExpandAll, canCollapseAll });
  }, [onExpansionChange, canExpandAll, canCollapseAll]);

  useImperativeHandle(
    ref,
    () => ({
      expandAll: () => {
        const all = new Set<string>();
        const walk = (list: FolderNode[]) => {
          for (const node of list) {
            if (showContracts || node.children.length > 0)
              all.add(node.folder.id);
            walk(node.children);
          }
        };
        walk(roots);
        setExpanded(all);
      },
      collapseAll: () => {
        setExpanded(new Set(rootIds));
      },
    }),
    [roots, rootIds, showContracts],
  );

  function toggle(folderId: string, open?: boolean) {
    setExpanded((current) => {
      const next = new Set(current);
      const willOpen = open ?? !next.has(folderId);
      if (willOpen) next.add(folderId);
      else next.delete(folderId);
      return next;
    });
  }

  // Retry refetches exactly one folder's tree query (D-15): other folders and the page-level
  // folder list are untouched.
  function retryFolder(folderId: string) {
    // The error row is replaced by a loading row while it refetches, so focus moves to its folder.
    focusItem(`f:${folderId}`);
    void queryClient.refetchQueries({
      queryKey: ["contracts", "folder-tree", folderId],
      exact: true,
    });
  }

  function openFolder(folderId: string) {
    setFocused({ key: `f:${folderId}`, ownerKey: `f:${folderId}` });
    void navigate({ to: "/folders/$folderId", params: { folderId } });
    onOpen?.();
  }

  function openContract(contractId: string) {
    void navigate({ to: "/contracts/$contractId", params: { contractId } });
    onOpen?.();
  }

  function openOverflow(folderId: string) {
    void navigate({
      to: "/folders/$folderId/contracts",
      params: { folderId },
      search: { subtree: false },
    });
    onOpen?.();
  }

  function onKeyDown(event: KeyboardEvent<HTMLElement>, row: TreeRow) {
    // Keys pressed on a row action or the Retry button belong to that control.
    if (event.target !== event.currentTarget) return;
    // Ctrl, Meta and Alt chords (Alt+Arrow is the browser's back and forward) belong to the browser
    // and assistive technology; the tree's keys are plain or Shift-modified.
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const position = focusableRows.findIndex(
      (candidate) => candidate.key === row.key,
    );
    let handled = true;
    switch (event.key) {
      case "ArrowDown": {
        const next = focusableRows[position + 1];
        if (next) focusItem(next.key);
        break;
      }
      case "ArrowUp": {
        const previous = focusableRows[position - 1];
        if (previous) focusItem(previous.key);
        break;
      }
      case "ArrowRight":
        // Only a folder row opens or is entered; contract, overflow and error rows have no children.
        if (row.kind === "folder") {
          const expandable = isExpandableFolder(row.node, contractsShown);
          if (expandable && !expanded.has(row.node.folder.id))
            toggle(row.node.folder.id, true);
          else {
            const child = firstChildKey(row.key, rows);
            if (child !== null) focusItem(child);
          }
        }
        break;
      case "ArrowLeft": {
        if (
          row.kind === "folder" &&
          (showContracts || row.node.children.length > 0) &&
          expanded.has(row.node.folder.id)
        ) {
          toggle(row.node.folder.id, false);
        } else {
          const parent = parentFolderKey(row, rows);
          if (parent !== null) focusItem(parent);
        }
        break;
      }
      case "Home": {
        const first = focusableRows[0];
        if (first) focusItem(first.key);
        break;
      }
      case "End": {
        const last = focusableRows.at(-1);
        if (last) focusItem(last.key);
        break;
      }
      case "Enter":
      case " ":
        if (row.kind === "folder") openFolder(row.node.folder.id);
        else if (row.kind === "contract") openContract(row.contract.id);
        else if (row.kind === "overflow") openOverflow(row.folderId);
        else if (row.kind === "error") retryFolder(row.folderId);
        break;
      default:
        if (event.key.length === 1 && /\S/.test(event.key)) {
          const index = rows.findIndex(
            (candidate) => candidate.key === row.key,
          );
          const match = typeAheadKey(rows, index, event.key);
          if (match !== null) focusItem(match);
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
    setFocused({
      key: `f:${node.folder.id}`,
      ownerKey: `f:${node.folder.id}`,
    });
    if (showContracts || node.children.length > 0) toggle(node.folder.id);
  }

  // A click on a contract or overflow row opens it, unless it landed on the row's own link (the
  // link navigates itself, and keeps middle-click and the context menu working).
  function onOpenClick(event: MouseEvent<HTMLElement>, open: () => void) {
    event.stopPropagation();
    if ((event.target as HTMLElement).closest("a, button")) return;
    open();
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
    const expandable = isExpandableFolder(node, contractsShown);
    const isOpen = expanded.has(folder.id);
    const current = folder.id === currentId;
    const sibling = siblings.get(folder.id);
    return (
      <div
        key={row.key}
        {...rowFocusProps(row)}
        tabIndex={row.key === tabStopKey ? 0 : -1}
        role="row"
        aria-level={node.depth + 1}
        aria-setsize={sibling?.size}
        aria-posinset={sibling?.position}
        aria-expanded={expandable ? isOpen : undefined}
        aria-selected={current}
        aria-current={current ? "page" : undefined}
        aria-labelledby={`${domId(row.key)}-label`}
        className={cx(styles.gridRow, styles.item, current && styles.current)}
        onKeyDown={(event) => {
          onKeyDown(event, row);
        }}
        onClick={(event) => {
          onClick(event, node);
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
            id={`${domId(row.key)}-label`}
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
            {renderActions(node, row.key === tabStopKey)}
          </span>
        ) : null}
      </div>
    );
  }

  // A contract is a treegrid row one level below its folder (the treegrid's "treeitem"): its name
  // is a real link, so click, middle-click and the context menu all open the contract.
  function renderContractRow(row: Extract<TreeRow, { kind: "contract" }>) {
    const { contract } = row;
    const rowId = domId(row.key);
    const displayName = contractDisplayName(contract);
    const named = contract.name !== null && contract.name !== "";
    const tabbable = row.key === tabStopKey;
    return (
      <div
        key={row.key}
        {...rowFocusProps(row)}
        tabIndex={tabbable ? 0 : -1}
        role="row"
        aria-level={row.depth + 1}
        aria-setsize={row.setSize}
        aria-posinset={row.position}
        aria-labelledby={`${rowId}-label`}
        className={cx(styles.gridRow, styles.contractRow)}
        onKeyDown={(event) => {
          onKeyDown(event, row);
        }}
        onClick={(event) => {
          onOpenClick(event, () => {
            openContract(contract.id);
          });
        }}
      >
        {/* The folder columns (kind, counts) say nothing about a contract: the name cell spans
            them up to the actions column, so a long name has room and the meta follows it. */}
        <span
          role="gridcell"
          aria-colspan={cols.length > 0 ? cols.length + 1 : undefined}
          className={cx(
            styles.row,
            hasActions ? styles.spanToActions : styles.spanAll,
          )}
          style={indent(row.depth)}
        >
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
          {renderContractMeta ? (
            // Right after the name, like the contract page header; a narrow tree wraps it under
            // the name instead of squeezing the name.
            <span className={styles.contractMeta} data-contract-meta="">
              {renderContractMeta(contract)}
            </span>
          ) : null}
        </span>
        {hasActions ? (
          <span
            role="gridcell"
            className={cx(styles.cell, styles.end, styles.actions)}
            data-tree-action=""
          >
            {renderContractActions?.(contract, tabbable)}
          </span>
        ) : null}
      </div>
    );
  }

  // Loading, error and overflow rows trail the contracts of one folder (UI-SPEC Row type
  // table). Loading only reports a state and are not focusable; error and overflow are
  // treegrid rows. The error row is inline copy with a Retry (not a ProblemAlert: a block alert
  // would break the 40px row geometry; recorded exception to the ui-frontend skill rule 3).
  function renderStateRow(
    row: Exclude<TreeRow, { kind: "folder" | "contract" }>,
  ) {
    const geometry = cx(styles.gridRow, styles.stateRow);
    const nameCell = (content: ReactNode) => (
      <span role="gridcell" className={styles.row} style={indent(row.depth)}>
        <span className={cx(styles.toggle, styles.leaf)} aria-hidden="true" />
        {content}
      </span>
    );
    if (row.kind === "loading") {
      return (
        <div
          key={row.key}
          role="row"
          aria-level={row.depth + 1}
          aria-busy="true"
          className={geometry}
        >
          {nameCell(<Skeleton label="contracts" />)}
        </div>
      );
    }
    if (row.kind === "error") {
      return (
        <div
          key={row.key}
          {...rowFocusProps(row)}
          tabIndex={row.key === tabStopKey ? 0 : -1}
          role="row"
          aria-level={row.depth + 1}
          className={cx(geometry, styles.item)}
          onKeyDown={(event) => {
            onKeyDown(event, row);
          }}
        >
          {nameCell(
            <>
              <AlertTriangle
                size={14}
                strokeWidth={2}
                aria-hidden="true"
                className={styles.errorIcon}
              />
              <span className={styles.label}>{treeCopy.error}</span>
              <Button
                variant="secondary"
                size="small"
                tabIndex={row.key === tabStopKey ? 0 : -1}
                onClick={() => {
                  retryFolder(row.folderId);
                }}
              >
                {treeCopy.retry}
              </Button>
            </>,
          )}
        </div>
      );
    }
    return (
      <div
        key={row.key}
        {...rowFocusProps(row)}
        tabIndex={row.key === tabStopKey ? 0 : -1}
        role="row"
        aria-level={row.depth + 1}
        className={cx(geometry, styles.item)}
        onKeyDown={(event) => {
          onKeyDown(event, row);
        }}
        onClick={(event) => {
          onOpenClick(event, () => {
            openOverflow(row.folderId);
          });
        }}
      >
        {nameCell(
          <>
            <span className={styles.label}>
              <Link
                to="/folders/$folderId/contracts"
                params={{ folderId: row.folderId }}
                search={{ subtree: false }}
                tabIndex={-1}
                className={styles.contractLink}
              >
                {treeCopy.overflow}
              </Link>
            </span>
            <ArrowRight
              size={14}
              strokeWidth={2}
              aria-hidden="true"
              className={styles.overflowIcon}
            />
          </>,
        )}
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
