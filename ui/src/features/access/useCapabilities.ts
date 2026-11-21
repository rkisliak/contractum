// What the caller may do at a folder (`GET /identity/me/capabilities?folder_id=`), or globally
// when no folder is given. This only hides or disables controls (D-UI-08-21): the API re-checks
// every write, so a stale or missing answer is never a security problem.
import { useQuery } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import type { Capabilities } from "@/api/types";

export function capabilitiesQueryKey(folderId: string | null | undefined) {
  return ["capabilities", folderId ?? "global"] as const;
}

export type CapabilityCheck = {
  /** True when the permission is held. False while loading or after an error. */
  can: (permission: string) => boolean;
  data: Capabilities | undefined;
  isPending: boolean;
};

export function useCapabilities(
  folderId?: string | null,
  { enabled = true }: { enabled?: boolean } = {},
): CapabilityCheck {
  const { api } = useRouteContext({ from: "__root__" });
  const query = useQuery({
    queryKey: capabilitiesQueryKey(folderId),
    enabled,
    queryFn: async (): Promise<Capabilities> => {
      const { data } = await api.GET("/api/v1/identity/me/capabilities", {
        params: { query: folderId ? { folder_id: folderId } : {} },
      });
      if (data === undefined)
        throw new Error("GET /identity/me/capabilities had no body");
      return data;
    },
    staleTime: 30_000,
  });
  const permissions = query.data?.permissions;
  return {
    can: (permission) => permissions?.includes(permission) ?? false,
    data: query.data,
    isPending: query.isPending,
  };
}
