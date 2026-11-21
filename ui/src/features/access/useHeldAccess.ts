// What the caller holds anywhere: the permissions granted at ROOT (`/me` global permissions) plus
// the capabilities at every folder holding one of its role assignments (the same cache keys as
// `useCapabilities`, so folder pages reuse them). The shell navigation and the home page read it to
// hide areas the caller cannot use. It only hides: the API re-checks every request (D-UI-08-21).
import { useQueries } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import type { Capabilities, Me } from "@/api/types";
import { PERMISSION_TARGETS, type PermissionId } from "./permissionRegistry";
import { capabilitiesQueryKey } from "./useCapabilities";
import { useMe } from "./useMe";

export type HeldAccess = {
  me: Me;
  /** Every permission held at ROOT or at (and below) a folder holding a grant. */
  permissions: ReadonlySet<string>;
  /**
   * The capability answer at each folder holding a grant (inherited roles included, so a ROOT grant
   * shows at every folder). Never unioned: it answers "what can I do in THIS folder and below".
   */
  byFolder: { folderId: string; permissions: readonly string[] }[];
};

/**
 * True for a permission every signed-in, non-restricted principal holds (dc_auth target
 * "principal", such as `folder:list` or `audit:list`) or a special one. These say nothing about
 * what the caller was granted, so they never make an area visible or count as a granted permission.
 */
export function isBaselinePermission(permission: string): boolean {
  const target = PERMISSION_TARGETS[permission as PermissionId] as
    (typeof PERMISSION_TARGETS)[PermissionId] | undefined;
  return target === "principal" || target === "special";
}

/** The global permissions that come from a ROOT-level role, without the baseline ones. */
export function grantedGlobalPermissions(me: Me): string[] {
  return me.global_permissions.filter(
    (permission) => !isBaselinePermission(permission),
  );
}

/** True when the caller holds any of the permissions anywhere (baseline ones never count). */
export function holdsAny(
  access: HeldAccess,
  ...permissions: string[]
): boolean {
  return permissions.some(
    (permission) =>
      !isBaselinePermission(permission) && access.permissions.has(permission),
  );
}

/** Builds a HeldAccess from `/me` and the per-folder capability answers (exported for tests). */
export function heldAccess(
  me: Me,
  folderPermissions: readonly (readonly string[])[] = [],
  folderIds: readonly string[] = [],
): HeldAccess {
  return {
    me,
    permissions: new Set([
      ...me.global_permissions,
      ...folderPermissions.flat(),
    ]),
    byFolder: folderPermissions.flatMap((permissions, index) => {
      const folderId = folderIds[index];
      return folderId === undefined ? [] : [{ folderId, permissions }];
    }),
  };
}

export type HeldAccessQuery = {
  /** Undefined until `/me` arrived and every per-folder capability request settled (a failed one
   * counts as holding nothing there). */
  data: HeldAccess | undefined;
  /** The first failed request, if any. */
  error: unknown;
  refetch: () => void;
};

export function useHeldAccess(): HeldAccessQuery {
  const { api } = useRouteContext({ from: "__root__" });
  const me = useMe();
  const folderIds = [
    ...new Set((me.data?.assignments ?? []).map((a) => a.folder_id)),
  ];
  const capabilities = useQueries({
    queries: folderIds.map((folderId) => ({
      queryKey: capabilitiesQueryKey(folderId),
      queryFn: async (): Promise<Capabilities> => {
        const { data } = await api.GET("/api/v1/identity/me/capabilities", {
          params: { query: { folder_id: folderId } },
        });
        if (data === undefined)
          throw new Error("GET /identity/me/capabilities had no body");
        return data;
      },
      staleTime: 30_000,
    })),
  });
  const failed = capabilities.find((query) => query.isError);
  const settled = capabilities.every((query) => !query.isPending);
  return {
    data:
      me.data !== undefined && settled
        ? heldAccess(
            me.data,
            capabilities.map((query) => query.data?.permissions ?? []),
            folderIds,
          )
        : undefined,
    error: me.error ?? failed?.error,
    refetch: () => {
      void me.refetch();
      for (const query of capabilities) void query.refetch();
    },
  };
}
