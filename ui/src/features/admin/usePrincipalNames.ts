// Display names for grant principals. Grants carry only `principal_type` and `principal_id`, so
// this reads every principal list the caller can read to its end (fetchAllPages, capped; failures
// are ignored) and maps ids to names. An unknown id is shown as the raw id. The local and Entra
// user lists need the root-level `user:read`, so they are only requested when `/me` holds it.
import { useQuery } from "@tanstack/react-query";
import { hasGlobal, useMe } from "@/features/access/useMe";
import { fetchAllPages, type Page } from "@/lib/fetchAllPages";
import { principalIdOf } from "./principal";
import { useApi } from "./shared";

type PageQuery = { limit: number; cursor?: string };

export function usePrincipalNames() {
  const api = useApi();
  const me = useMe();
  const readUsers = hasGlobal(me.data, "user:read");
  const query = useQuery({
    queryKey: ["identity", "principal-names", readUsers],
    enabled: me.data !== undefined,
    staleTime: 30_000,
    queryFn: async (): Promise<Record<string, string>> => {
      // One list read to its end; `allowed` false skips it (an empty list, no request).
      const listAll = <T>(
        allowed: boolean,
        call: (query: PageQuery) => Promise<{ data?: Page<T> }>,
      ) =>
        allowed
          ? fetchAllPages<T>(async (query) => (await call(query)).data)
          : Promise.resolve({ items: [] as T[], truncated: false });
      const [users, groups, accounts, entraUsers, entraGroups, sps] =
        await Promise.allSettled([
          listAll(readUsers, (query) =>
            api.GET("/api/v1/identity/users", { params: { query } }),
          ),
          listAll(true, (query) =>
            api.GET("/api/v1/identity/groups", { params: { query } }),
          ),
          listAll(true, (query) =>
            api.GET("/api/v1/identity/service-accounts", { params: { query } }),
          ),
          listAll(readUsers, (query) =>
            api.GET("/api/v1/identity/entra/users", { params: { query } }),
          ),
          listAll(true, (query) =>
            api.GET("/api/v1/identity/entra/groups", { params: { query } }),
          ),
          listAll(true, (query) =>
            api.GET("/api/v1/identity/entra/service-principals", {
              params: { query },
            }),
          ),
        ]);
      const names: Record<string, string> = {};
      const items = <T>(result: PromiseSettledResult<{ items: T[] }>): T[] =>
        result.status === "fulfilled" ? result.value.items : [];
      for (const u of items(users))
        names[principalIdOf.localUser(u.id)] =
          `${u.display_name} (${u.username})`;
      for (const g of items(groups))
        names[principalIdOf.localGroup(g.id)] = g.name;
      for (const a of items(accounts)) names[a.principal_id] = a.name;
      for (const u of items(entraUsers)) names[u.principal_id] = u.display_name;
      for (const g of items(entraGroups))
        names[principalIdOf.entraGroup(g.object_id)] = g.display_name;
      for (const sp of items(sps)) names[sp.principal_id] = sp.display_name;
      return names;
    },
  });
  return (principalId: string): string | undefined => query.data?.[principalId];
}
