// Builds the folder tree from the flat, permission-trimmed list (D-UI-09-01). When the caller can
// read ROOT it is the single top node ("Root"), holding the organizations, then projects, then
// teams. A folder whose parent the caller cannot read becomes a top-level node, so nothing visible
// is ever lost.
import type { ContractSummary, Folder } from "@/api/types";
import type { FolderContractsState } from "./api";

export type FolderNode = {
  folder: Folder;
  children: FolderNode[];
  depth: number;
};

function byName(a: FolderNode, b: FolderNode): number {
  return a.folder.display_name.localeCompare(b.folder.display_name);
}

export function buildFolderTree(folders: Folder[]): FolderNode[] {
  const nodes = new Map<string, FolderNode>();
  for (const folder of folders) {
    nodes.set(folder.id, { folder, children: [], depth: 0 });
  }
  const roots: FolderNode[] = [];
  for (const node of nodes.values()) {
    const parent =
      node.folder.parent_id === null
        ? undefined
        : nodes.get(node.folder.parent_id);
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const settle = (list: FolderNode[], depth: number) => {
    list.sort(byName);
    for (const node of list) {
      node.depth = depth;
      settle(node.children, depth + 1);
    }
  };
  settle(roots, 0);
  return roots;
}

/** The ids of every ancestor of `folderId` that is in the list, nearest last. */
export function ancestorIds(folders: Folder[], folderId: string): string[] {
  const byId = new Map(folders.map((folder) => [folder.id, folder]));
  const chain: string[] = [];
  let current = byId.get(folderId);
  const seen = new Set<string>();
  while (current?.parent_id && !seen.has(current.parent_id)) {
    seen.add(current.parent_id);
    const parent = byId.get(current.parent_id);
    if (!parent) break;
    chain.unshift(parent.id);
    current = parent;
  }
  return chain;
}

/** Flattens the expanded part of the tree in display order (keyboard navigation). */
export function visibleNodes(
  roots: FolderNode[],
  expanded: ReadonlySet<string>,
): FolderNode[] {
  const out: FolderNode[] = [];
  const walk = (list: FolderNode[]) => {
    for (const node of list) {
      out.push(node);
      if (expanded.has(node.folder.id)) walk(node.children);
    }
  };
  walk(roots);
  return out;
}

/** "Root" for ROOT, otherwise the display name. */
export function folderName(
  folder: Pick<Folder, "kind" | "display_name">,
): string {
  return folder.kind === "root" ? ROOT_LABEL : folder.display_name;
}

export const ROOT_LABEL = "Root";

/**
 * One row of the tree when contracts are shown (D-11). `key` is unique across kinds (a prefix plus
 * the folder or contract id), so rendering, focus and type-ahead never confuse a folder with a
 * contract. State rows (`overflow`, `loading`, `error`) trail the contracts of one folder.
 */
export type TreeRow =
  | { kind: "folder"; key: string; depth: number; node: FolderNode }
  | {
      kind: "contract";
      key: string;
      depth: number;
      contract: ContractSummary;
      folderId: string;
      /** 1-based among that folder's contracts only. */
      position: number;
      setSize: number;
    }
  | { kind: "overflow"; key: string; depth: number; folderId: string }
  | { kind: "loading"; key: string; depth: number; folderId: string }
  | { kind: "error"; key: string; depth: number; folderId: string };

export type TreeRowKind = TreeRow["kind"];

/**
 * Flattens the expanded part of the tree in display order. With `contracts` null (the toggle is
 * off) the rows are exactly the folder rows of `visibleNodes`. Otherwise each expanded folder emits
 * its subfolders first, then its contracts in the server's order (never re-sorted), then one
 * trailing state row. A folder with no entry in `contracts` emits nothing of its own.
 */
export function visibleRows(
  roots: FolderNode[],
  expanded: ReadonlySet<string>,
  contracts: Readonly<Record<string, FolderContractsState>> | null,
): TreeRow[] {
  const out: TreeRow[] = [];
  const walk = (list: FolderNode[]) => {
    for (const node of list) {
      const id = node.folder.id;
      out.push({
        kind: "folder",
        key: `f:${id}`,
        depth: node.depth,
        node,
      });
      if (!expanded.has(id)) continue;
      walk(node.children);
      const state = contracts?.[id];
      if (state === undefined) continue;
      const depth = node.depth + 1;
      if (state.status === "loading") {
        out.push({ kind: "loading", key: `l:${id}`, depth, folderId: id });
      } else if (state.status === "error") {
        out.push({ kind: "error", key: `e:${id}`, depth, folderId: id });
      } else {
        state.items.forEach((contract, index) => {
          out.push({
            kind: "contract",
            key: `c:${contract.id}`,
            depth,
            contract,
            folderId: id,
            position: index + 1,
            setSize: state.items.length,
          });
        });
        // A folder with no contracts adds no row: an empty folder simply shows nothing under it.
        if (state.more) {
          out.push({ kind: "overflow", key: `o:${id}`, depth, folderId: id });
        }
      }
    }
  };
  walk(roots);
  return out;
}

/** The name a contract row shows: its ODCS name, or its id when the name is empty (`||`, not `??`). */
export function contractDisplayName(
  contract: Pick<ContractSummary, "name" | "odcs_id">,
): string {
  return contract.name || contract.odcs_id;
}

/** What type-ahead matches: the folder name or the contract name; state rows have none. */
export function rowLabel(row: TreeRow): string {
  if (row.kind === "folder") return folderName(row.node.folder);
  if (row.kind === "contract") return contractDisplayName(row.contract);
  return "";
}

/** Loading rows only report a state; every other row can hold the keyboard focus. */
export function isFocusableRow(row: TreeRow): boolean {
  return row.kind !== "loading";
}

/**
 * Whether a folder row can open: it has subfolders, or contracts are shown and its contract list
 * is not known to be empty (unknown until its list call returns, so it starts expandable).
 */
export function isExpandableFolder(
  node: FolderNode,
  contracts: Readonly<Record<string, FolderContractsState>> | null,
): boolean {
  if (node.children.length > 0) return true;
  if (contracts === null) return false;
  const state = contracts[node.folder.id];
  return !(state?.status === "ready" && state.items.length === 0);
}

/**
 * The key of the folder row one step up: a contract or state row belongs to its folder, a folder
 * row to its parent folder when that parent's row is visible (otherwise there is nowhere to go).
 */
export function parentFolderKey(
  row: TreeRow,
  rows: readonly TreeRow[],
): string | null {
  if (row.kind !== "folder") return `f:${row.folderId}`;
  const parentId = row.node.folder.parent_id;
  if (parentId === null) return null;
  const key = `f:${parentId}`;
  return rows.some((candidate) => candidate.key === key) ? key : null;
}

/**
 * The first focusable row directly under a folder row (one level deeper, before the next row at
 * the folder's own level or above), or null when it has none: a folder whose only child row is
 * loading, a closed one, or one that is not in `rows`.
 */
export function firstChildKey(
  folderKey: string,
  rows: readonly TreeRow[],
): string | null {
  const start = rows.findIndex((row) => row.key === folderKey);
  const folder = rows[start];
  if (folder === undefined) return null;
  for (const row of rows.slice(start + 1)) {
    if (row.depth <= folder.depth) return null;
    if (row.depth === folder.depth + 1 && isFocusableRow(row)) return row.key;
  }
  return null;
}

/**
 * Type-ahead: the key of the next focusable row after `fromIndex` (wrapping, and ending on the
 * starting row itself) whose label starts with `char`, ignoring case; null when none does.
 */
export function typeAheadKey(
  rows: readonly TreeRow[],
  fromIndex: number,
  char: string,
): string | null {
  const wanted = char.toLowerCase();
  const ordered = [
    ...rows.slice(fromIndex + 1),
    ...rows.slice(0, fromIndex + 1),
  ];
  const match = ordered.find(
    (row) =>
      isFocusableRow(row) && rowLabel(row).toLowerCase().startsWith(wanted),
  );
  return match?.key ?? null;
}
