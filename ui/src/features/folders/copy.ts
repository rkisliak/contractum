// Folder explorer strings (Copywriting Contract): one module, so an i18n pass has one place.
import type { FolderBlocker, FolderPurge } from "@/api/types";
import { kindLabel } from "@/features/access/roles";

const BLOCKER_NOUNS: Record<FolderBlocker["kind"], [string, string]> = {
  archived_ancestor: ["archived folder above", "archived folders above"],
  subfolders: ["subfolder", "subfolders"],
  contracts: ["contract", "contracts"],
  templates: ["template", "templates"],
  rule_sets: ["rule set", "rule sets"],
  subscriber_cards: ["subscriber card", "subscriber cards"],
  groups: ["group", "groups"],
  service_accounts: ["service account", "service accounts"],
  entra_registrations: ["Entra registration", "Entra registrations"],
};

const PURGE_NOUNS: Record<FolderPurge["kind"], [string, string]> = {
  deleted_contracts: ["deleted contract", "deleted contracts"],
  archived_subscriber_cards: [
    "archived subscriber card",
    "archived subscriber cards",
  ],
  unused_rule_sets: ["unused rule set", "unused rule sets"],
};

function phrase(noun: [string, string], count: number): string {
  return `${String(count)} ${count === 1 ? noun[0] : noun[1]}`;
}

/** "2 subfolders", "1 contract": the counts the delete check reports. */
export function blockerPhrase(blocker: FolderBlocker): string {
  return phrase(BLOCKER_NOUNS[blocker.kind], blocker.count);
}

/** "2 deleted contracts and 1 unused rule set": what goes with the folder. */
export function purgePhrase(purges: FolderPurge[]): string {
  const parts = purges.map((purge) =>
    phrase(PURGE_NOUNS[purge.kind], purge.count),
  );
  if (parts.length <= 1) return parts.join("");
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1] ?? ""}`;
}

export const folderCopy = {
  archivedBanner:
    "This folder is archived. It and everything below it are read-only. Reads keep working.",
  archiveTitle: (name: string) => `Archive ${name}?`,
  archiveBody:
    "It and everything below become read-only. Reads keep working. A Global admin can unarchive.",
  unarchiveTitle: (name: string) => `Unarchive ${name}?`,
  unarchiveBody:
    "It and everything below become writable again for everyone with the right roles.",
  deleteLabel: "Delete folder…",
  deleteTitle: (name: string) => `Delete ${name}?`,
  deleteBody: (path: string, purges: FolderPurge[]) =>
    `${path === "" ? "This folder" : `The folder ${path}`} is empty and will be deleted for good. This cannot be undone; its audit history is kept.${purges.length > 0 ? ` Also permanently removes ${purgePhrase(purges)}.` : ""}`,
  deleteToast: (name: string) => `Deleted ${name}.`,
  deleteChecking: "Checking what this folder contains…",
  deleteCheckFailed:
    "Delete is unavailable right now because the folder contents could not be checked.",
  deleteUnavailable: (blockers: FolderBlocker[]) => {
    if (blockers.some((blocker) => blocker.kind === "archived_ancestor"))
      return "Delete is unavailable while a folder above this one is archived. Unarchive it first.";
    return `Delete is unavailable while this folder contains: ${blockers.map(blockerPhrase).join(", ")}. Move or delete them first.`;
  },
  createLabel: (kind: string) => `Create ${kindLabel(kind).toLowerCase()}`,
  slugHelper:
    "Lowercase letters, digits and single hyphens. The slug is part of the folder path and cannot be changed later.",
} as const;

// "Show contracts in tree" strings (16-UI-SPEC Copywriting Contract). A folder with no visible
// contracts shows no row and no arrow, never a "No contracts" line.
export const treeCopy = {
  toggle: "Show contracts in tree",
  expandAll: "Expand all",
  collapseAll: "Collapse all",
  actionsLabel: "Tree actions",
  openContract: (name: string) => `Open ${name}`,
  overflow: "More contracts — open folder",
  loading: "Loading contracts",
  error: "Could not load contracts.",
  retry: "Retry loading",
  notPublished: "Not published",
  noLatest: "No Latest version",
  /** After a non-Latest head's badge (a draft of a published contract): "Latest 1.2.0". */
  latestVersion: (semver: string) => `Latest ${semver}`,
  /** The accessible name of a contract row: "{name}, {badges}, version {semver}". */
  rowName: (displayName: string, badges: string, versionText: string) =>
    `${displayName}, ${badges}, ${versionText}`,
} as const;
