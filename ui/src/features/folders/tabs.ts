// The folder page tabs (D-UI-09-03), a registry like nav.ts: later builders append an entry for
// their child route (templates, rule-sets, cards, access) instead of editing the folder layout.
// `segment` is the child path under /folders/$folderId ("" is the index, Overview).
import type { Folder } from "@/api/types";

export type FolderTab = {
  id: string;
  label: string;
  segment: string;
  /** Hides the tab for folders where it has no function. */
  visible?: (folder: Folder) => boolean;
  /**
   * Hides the tab once the caller's capabilities at the folder are known and hold none of these
   * (any of). Only hides: the API still checks every request.
   */
  requires?: string[];
};

/** Whether the tab shows for this folder and these capabilities (shown while they load). */
export function tabVisible(
  tab: FolderTab,
  folder: Folder,
  caps: { isPending: boolean; can: (permission: string) => boolean },
): boolean {
  if (tab.visible !== undefined && !tab.visible(folder)) return false;
  if (tab.requires === undefined || caps.isPending) return true;
  return tab.requires.some((permission) => caps.can(permission));
}

export const FOLDER_TABS: FolderTab[] = [
  { id: "overview", label: "Overview", segment: "" },
  {
    id: "contracts",
    label: "Contracts",
    segment: "contracts",
  },
  {
    id: "folders",
    label: "Folders",
    segment: "folders",
    visible: (folder) => folder.kind !== "team",
  },
  {
    id: "templates",
    label: "Templates",
    segment: "templates",
    // Defined here needs template:read; Available here needs contract:create.
    requires: ["template:read", "contract:create"],
  },
  {
    id: "rule-sets",
    label: "Rule sets",
    segment: "rule-sets",
    requires: ["ruleset:read", "ruleset:read-published"],
  },
  {
    id: "cards",
    label: "Subscribers",
    segment: "cards",
    visible: (folder) => folder.kind !== "root",
  },
  // Phase 12: grants, block inheritance and effective access.
  { id: "access", label: "Access", segment: "access" },
];
