// Builds the folder tree from the flat, permission-trimmed list (D-UI-09-01). When the caller can
// read ROOT it is the single top node ("Root"), holding the organizations, then projects, then
// teams. A folder whose parent the caller cannot read becomes a top-level node, so nothing visible
// is ever lost.
import type { Folder } from "@/api/types";

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
