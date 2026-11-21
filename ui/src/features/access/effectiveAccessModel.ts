// Turns the API answers (`/me` assignments, a folder's effective access and the folder list) into
// the plain-language model `EffectiveAccess` renders: which grant each held role comes from and
// the inheritance sentence. Pure functions, no access decision: the roles and the block come from
// the server; the assignments only explain where each role was granted.
import type { EffectiveAccess, Folder, Me } from "@/api/types";
import { ancestorIds } from "@/features/folders/tree";
import type {
  FolderRef,
  HeldRole,
  InheritanceState,
  RoleSource,
} from "./EffectiveAccess";

/** "Acme / Payments / Ledger" from the folder list, "Root" for ROOT, else the slug path. */
export function readableFolderPath(
  folderId: string,
  folders: Folder[],
  fallbackPath = "",
): string {
  const byId = new Map(folders.map((folder) => [folder.id, folder]));
  const folder = byId.get(folderId);
  if (folder === undefined) return fallbackPath === "" ? "Root" : fallbackPath;
  if (folder.kind === "root") return "Root";
  const names = [...ancestorIds(folders, folderId), folderId]
    .map((id) => byId.get(id))
    .filter((f): f is Folder => f !== undefined && f.kind !== "root")
    .map((f) => f.display_name);
  return names.join(" / ");
}

function folderRef(id: string, folders: Folder[]): FolderRef {
  return { id, label: readableFolderPath(id, folders) };
}

function isAncestorPath(ancestor: string, path: string): boolean {
  return (
    ancestor === "" || path === ancestor || path.startsWith(`${ancestor}/`)
  );
}

/** The held roles at a folder, each with the grants (on it or above) it comes from. */
export function folderRoles(
  me: Me | undefined,
  folder: Folder,
  folders: Folder[],
  access: EffectiveAccess,
): HeldRole[] {
  const chain = [...ancestorIds(folders, folder.id), folder.id];
  const blockedAt = access.inheritance_blocked_at;
  const blockIndex = blockedAt === null ? -1 : chain.indexOf(blockedAt);
  const groupNames = new Map(
    (me?.groups ?? []).map((group) => [group.id, group.display_name]),
  );
  const applies = (assignment: Me["assignments"][number]): boolean => {
    const index = chain.indexOf(assignment.folder_id);
    if (index < 0 && !isAncestorPath(assignment.folder_path, folder.path))
      return false;
    if (!access.inheritance_blocked || assignment.role === "global_admin")
      return true;
    // Blocked: only grants at or below the block reach here (a Global admin is exempt).
    return blockIndex >= 0 && index >= blockIndex;
  };
  const assignments = (me?.assignments ?? []).filter(applies);
  return access.roles.map((role) => ({
    role,
    sources: assignments
      .filter((assignment) => assignment.role === role)
      .map((assignment): RoleSource => ({
        folderId: assignment.folder_id,
        label: readableFolderPath(
          assignment.folder_id,
          folders,
          assignment.folder_path,
        ),
        here: assignment.folder_id === folder.id,
        viaGroup:
          assignment.via_group_id === null
            ? null
            : (groupNames.get(assignment.via_group_id) ?? "a group"),
      })),
  }));
}

/** The inheritance sentence's state at a folder. */
export function folderInheritance(
  folder: Folder,
  folders: Folder[],
  access: Pick<
    EffectiveAccess,
    "inheritance_blocked" | "inheritance_blocked_at"
  >,
): InheritanceState {
  if (folder.kind === "root") return { kind: "top" };
  if (access.inheritance_blocked) {
    const at = access.inheritance_blocked_at;
    return {
      kind: "blocked",
      at: at === null ? null : folderRef(at, folders),
      here: at === folder.id,
    };
  }
  return {
    kind: "open",
    ancestors: ancestorIds(folders, folder.id)
      .reverse()
      .map((id) => folderRef(id, folders)),
  };
}

/** Every role the principal holds anywhere, each with the folders it was granted on. */
export function homeRoles(me: Me, folders: Folder[]): HeldRole[] {
  const groupNames = new Map(
    me.groups.map((group) => [group.id, group.display_name]),
  );
  const byRole = new Map<string, RoleSource[]>();
  for (const assignment of me.assignments) {
    const sources = byRole.get(assignment.role) ?? [];
    sources.push({
      folderId: assignment.folder_id,
      label: readableFolderPath(
        assignment.folder_id,
        folders,
        assignment.folder_path,
      ),
      here: false,
      viaGroup:
        assignment.via_group_id === null
          ? null
          : (groupNames.get(assignment.via_group_id) ?? "a group"),
    });
    byRole.set(assignment.role, sources);
  }
  return [...byRole.entries()].map(([role, sources]) => ({ role, sources }));
}
