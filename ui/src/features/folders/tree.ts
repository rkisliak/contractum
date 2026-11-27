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
 * contract. State rows (`overflow`, `loading`, `error`, `empty`) trail the contracts of one folder.
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
  | { kind: "error"; key: string; depth: number; folderId: string }
  | { kind: "empty"; key: string; depth: number; folderId: string };

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
        if (state.items.length === 0) {
          out.push({ kind: "empty", key: `n:${id}`, depth, folderId: id });
        } else if (state.more) {
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

// RED stubs: the real helpers arrive in the GREEN commit.
export function isFocusableRow(_row: TreeRow): boolean {
  return false;
}
export function parentFolderKey(
  _row: TreeRow,
  _rows: readonly TreeRow[],
): string | null {
  return null;
}
export function firstChildKey(
  _folderKey: string,
  _rows: readonly TreeRow[],
): string | null {
  return null;
}
export function typeAheadKey(
  _rows: readonly TreeRow[],
  _fromIndex: number,
  _char: string,
): string | null {
  return null;
}
