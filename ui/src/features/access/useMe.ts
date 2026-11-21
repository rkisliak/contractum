// The signed-in principal (`GET /identity/me`), shared by the shell, home and every screen that
// gates on global permissions. One query key, ["me"], so every caller reads the same cache entry.
import { useQuery } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import type { Me } from "@/api/types";

export const meQueryKey = ["me"] as const;

export function useMe() {
  const { api } = useRouteContext({ from: "__root__" });
  return useQuery({
    queryKey: meQueryKey,
    queryFn: async (): Promise<Me> => {
      const { data } = await api.GET("/api/v1/identity/me");
      if (data === undefined) throw new Error("GET /identity/me had no body");
      return data;
    },
    staleTime: 60_000,
  });
}

/** True when `/me` grants the permission globally (from a ROOT-level role). */
export function hasGlobal(me: Me | undefined, permission: string): boolean {
  return me?.global_permissions.includes(permission) ?? false;
}
