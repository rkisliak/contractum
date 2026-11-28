// Folder reads and writes over the typed client. Every folder query key starts with "folders",
// so one invalidation refreshes the tree, the folder page and its children after a write.
import {
  queryOptions,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import type { components } from "@/api/schema.gen";
import type { ApiClient } from "@/api/client";
import type { ContractSummary, Folder, FolderDeletionCheck } from "@/api/types";
import { createLimiter } from "@/lib/limiter";

type FolderPage = components["schemas"]["IdentityFolderListOut"];

/** The tree pages through GET /folders up to this many folders (D-UI-09-01). */
export const FOLDER_CAP = 2000;
const PAGE = 100;

export const folderKeys = {
  all: ["folders"] as const,
  list: (includeArchived: boolean) =>
    ["folders", "list", { includeArchived }] as const,
  detail: (id: string) => ["folders", "detail", id] as const,
  // Under `folders`, so every folder write (and the delete itself) refreshes it.
  deletionCheck: (id: string) => ["folders", "deletion-check", id] as const,
};

function useApi() {
  return useRouteContext({ from: "__root__" }).api;
}

export type FolderList = { folders: Folder[]; truncated: boolean };

/** Contracts counted per folder row: one list page, so a count past this reads "100+". */
export const CONTRACT_COUNT_PAGE = 100;

export type ContractCount = { count: number; more: boolean };

/**
 * Every contract-list call the folder navigator makes (the count column and the tree's folder
 * pages) goes through this limiter: one call per visible row, all at once, would queue up behind
 * each other in the services and the slowest would answer 503. Four at a time, as for member counts.
 */
const CONTRACT_LIST_CONCURRENCY = 4;
const limitedList = createLimiter(CONTRACT_LIST_CONCURRENCY);

/**
 * How many contracts the caller can see in a folder and below, from one page of the contracts
 * list (`include_subtree`). Only rendered (visible, expanded) tree rows call it, and the answer
 * is cached under `["contracts", ...]`, so contract writes refresh it.
 */
export function useContractCount(folderId: string) {
  const api = useApi();
  return useQuery({
    queryKey: ["contracts", "folder-count", folderId] as const,
    queryFn: async (): Promise<ContractCount> => {
      const { data } = await limitedList(() =>
        api.GET("/api/v1/contracts/contracts", {
          params: {
            query: {
              folder_id: folderId,
              include_subtree: true,
              limit: CONTRACT_COUNT_PAGE,
            },
          },
        }),
      );
      if (data === undefined) throw new Error("GET /contracts had no body");
      return { count: data.items.length, more: data.next_cursor !== null };
    },
    staleTime: 60_000,
    retry: false,
  });
}

/** One page of a folder's own contracts in the tree: the same size the Contracts column counts (D-13). */
export const FOLDER_TREE_PAGE = CONTRACT_COUNT_PAGE;

/** A folder's own contracts as the tree holds them: loading, failed, or one page and "more". */
export type FolderContractsState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; items: ContractSummary[]; more: boolean };

/**
 * The tree query for one folder (D-02): its own contracts only (`include_subtree=false`), at most
 * one page, in the server's name order (`sort=name`). Its key starts with "contracts", so a contract write refreshes
 * it; it cannot share the count column's entry, which asks for the subtree.
 */
export function folderContractsOptions(api: ApiClient, folderId: string) {
  return queryOptions({
    queryKey: ["contracts", "folder-tree", folderId] as const,
    queryFn: async () => {
      const { data } = await limitedList(() =>
        api.GET("/api/v1/contracts/contracts", {
          params: {
            query: {
              folder_id: folderId,
              include_subtree: false,
              sort: "name",
              limit: FOLDER_TREE_PAGE,
            },
          },
        }),
      );
      if (data === undefined) throw new Error("GET /contracts had no body");
      return { items: data.items, more: data.next_cursor !== null };
    },
    staleTime: 30_000,
    retry: false,
  });
}

/**
 * One list call per folder id, keyed by folder id. The caller passes only the expanded, visible
 * folders while "Show contracts in tree" is on; an empty array makes no request at all.
 */
export function useFolderContractQueries(
  folderIds: readonly string[],
): Record<string, FolderContractsState> {
  const api = useApi();
  return useQueries({
    queries: folderIds.map((folderId) => folderContractsOptions(api, folderId)),
    combine: (results) => {
      const states: Record<string, FolderContractsState> = {};
      results.forEach((result, index) => {
        const folderId = folderIds[index];
        if (folderId === undefined) return;
        states[folderId] =
          result.data !== undefined
            ? { status: "ready", ...result.data }
            : result.isError
              ? { status: "error" }
              : { status: "loading" };
      });
      return states;
    },
  });
}

/** Every folder the caller can read (already permission-trimmed by the server). */
export function useFolderList(includeArchived = false) {
  const api = useApi();
  return useQuery({
    queryKey: folderKeys.list(includeArchived),
    queryFn: async (): Promise<FolderList> => {
      const page = async (cursor: string | null): Promise<FolderPage> => {
        const { data } = await api.GET("/api/v1/identity/folders", {
          params: {
            query: {
              limit: PAGE,
              include_archived: includeArchived,
              ...(cursor === null ? {} : { cursor }),
            },
          },
        });
        if (data === undefined) throw new Error("GET /folders had no body");
        return data;
      };
      const folders: Folder[] = [];
      let cursor: string | null = null;
      do {
        const next = await page(cursor);
        folders.push(...next.items);
        cursor = next.next_cursor;
      } while (cursor !== null && folders.length < FOLDER_CAP);
      return { folders, truncated: cursor !== null };
    },
    staleTime: 30_000,
  });
}

export function useFolder(folderId: string) {
  const api = useApi();
  return useQuery({
    queryKey: folderKeys.detail(folderId),
    queryFn: async (): Promise<Folder> => {
      const { data } = await api.GET("/api/v1/identity/folders/{folder_id}", {
        params: { path: { folder_id: folderId } },
      });
      if (data === undefined) throw new Error("GET /folders/{id} had no body");
      return data;
    },
  });
}

/**
 * What blocks deleting a folder and what would be removed with it (`folder:delete` holders only).
 * Identity asks contracts and validation, so the caller enables it only for a non-ROOT folder the
 * caller may delete, and one folder at a time (never per tree row). A failure keeps the menu item
 * disabled with a reason.
 */
export function useFolderDeletionCheck(
  folderId: string,
  options: { enabled: boolean },
) {
  const api = useApi();
  return useQuery({
    queryKey: folderKeys.deletionCheck(folderId),
    enabled: options.enabled,
    queryFn: async (): Promise<FolderDeletionCheck> => {
      const { data } = await api.GET(
        "/api/v1/identity/folders/{folder_id}/deletion-check",
        { params: { path: { folder_id: folderId } } },
      );
      if (data === undefined)
        throw new Error("GET /folders/{id}/deletion-check had no body");
      return data;
    },
    staleTime: 30_000,
    retry: false,
  });
}

export function useFolderWrites() {
  const api = useApi();
  const queryClient = useQueryClient();
  const settle = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: folderKeys.all }),
      // Archiving changes what the caller may do below the folder.
      queryClient.invalidateQueries({ queryKey: ["capabilities"] }),
    ]);
  };

  const create = useMutation({
    mutationFn: async (input: {
      parentId: string;
      slug: string;
      displayName: string;
    }) => {
      const { data } = await api.POST("/api/v1/identity/folders", {
        body: {
          parent_id: input.parentId,
          slug: input.slug,
          display_name: input.displayName,
        },
      });
      if (data === undefined) throw new Error("POST /folders had no body");
      return data;
    },
    onSuccess: settle,
  });

  const rename = useMutation({
    mutationFn: async (input: { folderId: string; displayName: string }) => {
      const { data } = await api.PATCH("/api/v1/identity/folders/{folder_id}", {
        params: { path: { folder_id: input.folderId } },
        body: { display_name: input.displayName },
      });
      if (data === undefined) throw new Error("PATCH /folders had no body");
      return data;
    },
    onSuccess: settle,
  });

  const archive = useMutation({
    mutationFn: async (folderId: string) => {
      await api.POST("/api/v1/identity/folders/{folder_id}/archive", {
        params: { path: { folder_id: folderId } },
      });
    },
    onSuccess: settle,
  });

  const unarchive = useMutation({
    mutationFn: async (folderId: string) => {
      await api.POST("/api/v1/identity/folders/{folder_id}/unarchive", {
        params: { path: { folder_id: folderId } },
      });
    },
    onSuccess: settle,
  });

  // No invalidation here: the caller navigates away first and then settles, so the deleted
  // folder's own detail query never refetches into a 404.
  const remove = useMutation({
    mutationFn: async (folderId: string) => {
      await api.DELETE("/api/v1/identity/folders/{folder_id}", {
        params: { path: { folder_id: folderId } },
      });
    },
  });

  return { create, rename, archive, unarchive, remove, settle };
}
