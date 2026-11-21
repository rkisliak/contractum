// PrincipalName: the display name for a principal id (created_by, published_by, updated_by). Every
// id rendered in the same tick goes to identity in one POST /principals/names (at most 100 ids per
// call) and each answer is cached per id. Until the name arrives, and for an id identity does not
// name (unknown, removed, another namespace), it shows the short id; the full id is the tooltip.
import { useQuery } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import type { ApiClient } from "@/api/client";

const MAX_IDS = 100;

type Waiter = {
  resolve: (name: string | null) => void;
  reject: (error: unknown) => void;
};
type Batch = { waiting: Map<string, Waiter[]>; scheduled: boolean };

const batches = new WeakMap<ApiClient, Batch>();

async function flush(api: ApiClient, batch: Batch): Promise<void> {
  const entries = [...batch.waiting.entries()];
  batch.waiting = new Map();
  batch.scheduled = false;
  for (let start = 0; start < entries.length; start += MAX_IDS) {
    const chunk = entries.slice(start, start + MAX_IDS);
    try {
      const { data } = await api.POST("/api/v1/identity/principals/names", {
        body: { ids: chunk.map(([id]) => id) },
      });
      const names = new Map(
        (data?.items ?? []).map((item) => [item.id, item.display_name]),
      );
      for (const [id, waiters] of chunk)
        for (const waiter of waiters) waiter.resolve(names.get(id) ?? null);
    } catch (error) {
      for (const [, waiters] of chunk)
        for (const waiter of waiters) waiter.reject(error);
    }
  }
}

function loadName(api: ApiClient, id: string): Promise<string | null> {
  let batch = batches.get(api);
  if (batch === undefined) {
    batch = { waiting: new Map(), scheduled: false };
    batches.set(api, batch);
  }
  const current = batch;
  return new Promise((resolve, reject) => {
    const waiters = current.waiting.get(id) ?? [];
    waiters.push({ resolve, reject });
    current.waiting.set(id, waiters);
    if (!current.scheduled) {
      current.scheduled = true;
      queueMicrotask(() => {
        void flush(api, current);
      });
    }
  });
}

/** `local:3f2a…` → `local:3f2a1b9c`; the fallback when identity does not name the id. */
export function shortPrincipal(principal: string | null | undefined): string {
  if (!principal) return "Not set";
  const parts = principal.split(":");
  return parts.length > 1
    ? `${parts[0] ?? ""}:${(parts.at(-1) ?? "").slice(0, 8)}`
    : principal;
}

/** The display name for a principal id, or undefined while loading or when it is not named. */
export function usePrincipalName(
  id: string | null | undefined,
): string | undefined {
  const api = useRouteContext({ from: "__root__" }).api;
  const query = useQuery({
    queryKey: ["identity", "principal-name", id ?? ""],
    enabled: Boolean(id),
    staleTime: 5 * 60_000,
    retry: false,
    queryFn: () => loadName(api, id ?? ""),
  });
  return query.data ?? undefined;
}

/** The name as text, falling back to the short id (for strings such as a dialog message). */
export function usePrincipalLabel(id: string | null | undefined): string {
  return usePrincipalName(id) ?? shortPrincipal(id);
}

export function PrincipalName({ id }: { id: string | null | undefined }) {
  const name = usePrincipalName(id);
  if (!id) return <>Not set</>;
  return <span title={id}>{name ?? shortPrincipal(id)}</span>;
}
