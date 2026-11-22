// The folder tree-table (D-UI-09-02): the WAI-ARIA treegrid pattern (row focus) with a roving
// tabindex. Up and Down move through visible rows, Right expands or enters, Left collapses or goes to the parent, Home and
// End jump, Enter opens the folder, printable keys jump by name (type-ahead). A mouse click on a row
// expands or collapses it; the row's open action (the arrow) opens it. The current folder
// carries aria-current="page" and its ancestors are expanded.
import { useNavigate } from "@tanstack/react-router";
import { ChevronRight, Folder as FolderIcon } from "lucide-react";
import {
  useId,
  useMemo,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from "react";
import type { Folder } from "@/api/types";
import { Badge } from "@/components/Badge";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { cx } from "@/lib/cx";
import {
  ancestorIds,
  buildFolderTree,
  folderName,
  visibleNodes,
  type FolderNode,
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
};

export function FolderTree({
  folders,
  currentId,
  label = "Folders",
  onOpen,
  columns,
  actionsColumn,
  renderActions,
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
        if (hasChildren && !isOpen) toggle(id, true);
        else if (hasChildren && node.children[0])
          focusItem(node.children[0].folder.id);
        break;
      case "ArrowLeft":
        if (hasChildren && isOpen) toggle(id, false);
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
    if (node.children.length > 0) toggle(node.folder.id);
  }

  // The APG treegrid: a flat list of rows carrying aria-level/setsize/posinset, each with a name
  // cell, the fact cells and an actions cell. Row actions are real buttons inside a gridcell (a
  // tree may hold only treeitems, so nested buttons there broke aria-required-children). Each row
  // is named by its folder name only (aria-labelledby).
  const cols = columns ?? [];
  const hasActions = renderActions !== undefined;
  function renderItems(list: FolderNode[]): ReactNode[] {
    return list.flatMap((node, position) => {
      const { folder } = node;
      const hasChildren = node.children.length > 0;
      const isOpen = expanded.has(folder.id);
      const current = folder.id === currentId;
      return [
        <div
          key={folder.id}
          id={domId(folder.id)}
          role="row"
          aria-level={node.depth + 1}
          aria-setsize={list.length}
          aria-posinset={position + 1}
          aria-expanded={hasChildren ? isOpen : undefined}
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
          <span
            role="gridcell"
            className={styles.row}
            style={{
              paddingInlineStart: `calc(${String(node.depth)} * var(--spacing-3))`,
            }}
          >
            <span
              className={cx(styles.toggle, !hasChildren && styles.leaf)}
              data-tree-toggle={hasChildren ? "" : undefined}
              aria-hidden="true"
            >
              {hasChildren ? (
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
        </div>,
        ...(hasChildren && isOpen ? renderItems(node.children) : []),
      ];
    });
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
      {renderItems(roots)}
    </div>
  );
}

function expandedParent(node: FolderNode, visible: FolderNode[]): boolean {
  return visible.some(
    (candidate) => candidate.folder.id === node.folder.parent_id,
  );
}
