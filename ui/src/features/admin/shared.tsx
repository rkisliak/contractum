// Shared pieces of the access-administration and identity screens (Phase 12): the API handle,
// query keys and invalidation, status badges, owner names and options, client-side row filters
// and credential expiry helpers. Everything here only shapes the UI; the API re-checks writes.
import { useQueryClient } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import type { Folder, Me } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Checkbox, TextField } from "@/components/form";
import { useFolderList } from "@/features/folders/api";
import { folderName } from "@/features/folders/tree";
import styles from "./admin.module.css";

export { formatDateTime } from "@/features/folders/FolderOverview";

export function useApi() {
  return useRouteContext({ from: "__root__" }).api;
}

/** Every identity-admin query key starts with "identity". */
export const idKeys = {
  users: ["identity", "users"] as const,
  user: (id: string) => ["identity", "users", id] as const,
  groups: ["identity", "groups"] as const,
  group: (id: string) => ["identity", "groups", id] as const,
  members: (id: string) => ["identity", "groups", id, "members"] as const,
  grants: (folderId: string) => ["identity", "grants", folderId] as const,
  effective: (folderId: string) =>
    ["identity", "effective-access", folderId] as const,
  entraUsers: ["identity", "entra", "users"] as const,
  entraGroups: ["identity", "entra", "groups"] as const,
  entraSps: ["identity", "entra", "service-principals"] as const,
  entraDirectory: ["identity", "entra", "directory"] as const,
  serviceAccounts: ["identity", "service-accounts"] as const,
  serviceAccount: (id: string) => ["identity", "service-accounts", id] as const,
  keys: (id: string) => ["identity", "service-accounts", id, "keys"] as const,
  // Sessions of the caller ("me") or of one local user (admin, by user id).
  sessions: (scope: string) => ["identity", "sessions", scope] as const,
  myPats: ["identity", "pats", "mine"] as const,
  allPats: ["identity", "pats", "all"] as const,
  // Prefixes of the PrincipalName and usePrincipalNames query keys: a rename refreshes every
  // "by whom" cell and created/updated-by name on screen.
  principalName: ["identity", "principal-name"] as const,
  principalNames: ["identity", "principal-names"] as const,
};

/**
 * Invalidates the given keys plus `/me` and capabilities (an authz write can change what the
 * caller may do, D-UI-12-13). No optimistic updates: escalation guards can refuse.
 */
export function useSettle() {
  const queryClient = useQueryClient();
  return async (...keys: readonly (readonly unknown[])[]) => {
    await Promise.all([
      ...keys.map((queryKey) => queryClient.invalidateQueries({ queryKey })),
      queryClient.invalidateQueries({ queryKey: ["me"] }),
      queryClient.invalidateQueries({ queryKey: ["capabilities"] }),
    ]);
  };
}

export function ActiveBadge({ active }: { active: boolean }) {
  return active ? (
    <Badge intent="success">Active</Badge>
  ) : (
    <Badge intent="neutral">Deactivated</Badge>
  );
}

/** Folder display names by id, from the shared folder list (archived included). */
export function useFolderLookup() {
  const list = useFolderList(true);
  return useMemo(() => {
    const byId = new Map<string, Folder>();
    for (const folder of list.data?.folders ?? []) byId.set(folder.id, folder);
    return {
      folders: list.data?.folders ?? [],
      get: (id: string) => byId.get(id),
      /** "ROOT (global)" for the root, the folder path for others, else the id. */
      ownerLabel: (id: string) => {
        const folder = byId.get(id);
        if (folder === undefined) return id;
        if (folder.kind === "root") return "ROOT (global)";
        return folder.path === "" ? folderName(folder) : folder.path;
      },
    };
  }, [list.data]);
}

/**
 * Owners the caller may pick for a group, service account or Entra registration: ROOT when the
 * permission is global, plus every Organization they administer (all of them for a Global admin).
 */
export function ownerOptions(
  me: Me | undefined,
  folders: Folder[],
  globalPermission: string,
): { value: string; label: string }[] {
  if (me === undefined) return [];
  const isGlobal =
    me.global_permissions.includes(globalPermission) ||
    me.assignments.some((assignment) => assignment.role === "global_admin");
  const root = folders.find((folder) => folder.kind === "root");
  const adminOf = new Set(
    me.assignments
      .filter((assignment) => assignment.role === "org_admin")
      .map((assignment) => assignment.folder_id),
  );
  const orgs = folders.filter(
    (folder) =>
      folder.kind === "organization" &&
      !folder.archived &&
      (isGlobal || adminOf.has(folder.id)),
  );
  return [
    ...(isGlobal && root ? [{ value: root.id, label: "ROOT (global)" }] : []),
    ...orgs.map((folder) => ({ value: folder.id, label: folder.display_name })),
  ];
}

/** Client-side filter over loaded rows plus the "Hide deactivated" toggle (D-UI-12-02). */
export function useRowFilter<T>(
  rows: T[],
  text: (row: T) => string,
  isActive?: (row: T) => boolean,
) {
  const [query, setQuery] = useState("");
  const [hideInactive, setHideInactive] = useState(false);
  const needle = query.trim().toLowerCase();
  const filtered = rows.filter(
    (row) =>
      (needle === "" || text(row).toLowerCase().includes(needle)) &&
      (!hideInactive || isActive === undefined || isActive(row)),
  );
  return { query, setQuery, hideInactive, setHideInactive, filtered };
}

export function RowFilter({
  id,
  filter,
  withInactive = true,
}: {
  id: string;
  filter: {
    query: string;
    setQuery: (value: string) => void;
    hideInactive: boolean;
    setHideInactive: (value: boolean) => void;
  };
  withInactive?: boolean;
}) {
  return (
    <div className={styles.toolbar}>
      <div className={styles.toolbarField}>
        <TextField
          id={`${id}-filter`}
          label="Filter loaded rows"
          type="search"
          autoComplete="off"
          controlSize="small"
          value={filter.query}
          onChange={(event) => {
            filter.setQuery(event.target.value);
          }}
        />
      </div>
      {withInactive ? (
        <Checkbox
          id={`${id}-hide-inactive`}
          label="Hide deactivated"
          checked={filter.hideInactive}
          onChange={(event) => {
            filter.setHideInactive(event.target.checked);
          }}
        />
      ) : null}
    </div>
  );
}

/**
 * The default ceiling on credential lifetimes (identity's `credential_max_ttl_days`). A Global
 * admin can change it (Settings > Sign-in & security): the effective value is
 * `credential_max_ttl_days` of `/settings/public`, and this constant is only the fallback.
 */
export const MAX_CREDENTIAL_DAYS = 365;

/** `YYYY-MM-DD` for today plus `days`, in local time (a date input's value). */
export function dateInDays(days: number, now: Date = new Date()): string {
  const date = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate() + days,
  );
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/**
 * The `expires_at` sent for a picked date: the end of that day in local time, as ISO. Null for
 * a value that is not a date, in the past, or beyond the cap.
 */
export function expiresAtFor(
  value: string,
  now: Date = new Date(),
  maxDays: number = MAX_CREDENTIAL_DAYS,
): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) return null;
  const [, y, m, d] = match;
  const end = new Date(Number(y), Number(m) - 1, Number(d), 23, 59, 0);
  if (Number.isNaN(end.getTime()) || end.getTime() <= now.getTime())
    return null;
  if (value > dateInDays(maxDays, now)) return null;
  return end.toISOString();
}

/** Whole days until `iso` (negative once past). */
export function daysUntil(iso: string, now: Date = new Date()): number {
  return Math.ceil((new Date(iso).getTime() - now.getTime()) / 86_400_000);
}

export function CredentialStatus({ status }: { status: string }) {
  const intent =
    status === "active"
      ? "success"
      : status === "expired"
        ? "warning"
        : "neutral";
  const label = status.charAt(0).toUpperCase() + status.slice(1);
  return <Badge intent={intent}>{label}</Badge>;
}
