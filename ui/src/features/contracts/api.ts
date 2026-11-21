// Contract reads and writes over the typed client. Every contract query key starts with
// "contracts" (lists, shared with the folder Contracts tab) or ["contract", id] (one contract and
// everything under it), so a write invalidates exactly what it changed.
import { useQuery } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import { trimChar } from "@/lib/text";
import type { ApiClient } from "@/api/client";
import type {
  ContractDraft,
  ContractSummary,
  ContractVersion,
} from "@/api/types";

export const contractKeys = {
  lists: ["contracts"] as const,
  all: (id: string) => ["contract", id] as const,
  detail: (id: string) => ["contract", id, "detail"] as const,
  versions: (id: string) => ["contract", id, "versions"] as const,
  version: (id: string, ref: string) =>
    ["contract", id, "version", ref] as const,
  drafts: (id: string) => ["contract", id, "drafts"] as const,
  draft: (id: string, draftId: string) =>
    ["contract", id, "draft", draftId] as const,
  ruleSets: (id: string) => ["contract", id, "rule-sets"] as const,
  subscribers: (id: string) => ["contract", id, "subscribers"] as const,
};

export function useApi(): ApiClient {
  return useRouteContext({ from: "__root__" }).api;
}

export type DocFormat = "yaml" | "json";

export const ACCEPT: Record<DocFormat, string> = {
  yaml: "application/yaml",
  json: "application/json",
};

export function mediaTypeOf(format: DocFormat): string {
  return ACCEPT[format];
}

export function formatOf(mediaType: string | null | undefined): DocFormat {
  return mediaType?.includes("json") ? "json" : "yaml";
}

export type TextDocument = {
  text: string;
  etag: string | null;
  mediaType: string;
};

function textResult(
  data: unknown,
  response: Response,
  fallback: string,
): TextDocument {
  return {
    text: typeof data === "string" ? data : "",
    etag: response.headers.get("ETag"),
    mediaType: response.headers.get("Content-Type")?.split(";")[0] ?? fallback,
  };
}

/** The contract's current document (`/document`), in its stored format or converted by Accept. */
export async function fetchContractDocument(
  api: ApiClient,
  contractId: string,
  format?: DocFormat,
): Promise<TextDocument> {
  const { data, response } = await api.GET(
    "/api/v1/contracts/contracts/{contract_id}/document",
    {
      params: { path: { contract_id: contractId } },
      headers: format ? { Accept: ACCEPT[format] } : {},
      parseAs: "text",
    },
  );
  return textResult(data, response, "application/yaml");
}

export async function fetchDraftDocument(
  api: ApiClient,
  contractId: string,
  draftId: string,
  format?: DocFormat,
): Promise<TextDocument> {
  const { data, response } = await api.GET(
    "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/document",
    {
      params: { path: { contract_id: contractId, draft_id: draftId } },
      headers: format ? { Accept: ACCEPT[format] } : {},
      parseAs: "text",
    },
  );
  return textResult(data, response, "application/yaml");
}

export async function fetchVersionDocument(
  api: ApiClient,
  contractId: string,
  ref: string,
  format?: DocFormat,
): Promise<TextDocument> {
  const { data, response } = await api.GET(
    "/api/v1/contracts/contracts/{contract_id}/versions/{ref}",
    {
      params: { path: { contract_id: contractId, ref } },
      headers: format ? { Accept: ACCEPT[format] } : {},
      parseAs: "text",
    },
  );
  return textResult(data, response, "application/yaml");
}

export function useContract(contractId: string) {
  const api = useApi();
  return useQuery({
    queryKey: contractKeys.detail(contractId),
    queryFn: async (): Promise<ContractSummary> => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}",
        { params: { path: { contract_id: contractId } } },
      );
      if (data === undefined)
        throw new Error("GET /contracts/{id} had no body");
      return data;
    },
    retry: false,
  });
}

export function useContractVersions(contractId: string, enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: contractKeys.versions(contractId),
    queryFn: async (): Promise<ContractVersion[]> => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}/versions",
        { params: { path: { contract_id: contractId } } },
      );
      return data?.items ?? [];
    },
    enabled,
    retry: false,
  });
}

export function useDrafts(contractId: string, enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: contractKeys.drafts(contractId),
    queryFn: async (): Promise<ContractDraft[]> => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}/drafts",
        { params: { path: { contract_id: contractId } } },
      );
      return data?.items ?? [];
    },
    enabled,
    retry: false,
  });
}

/** Saves a Blob through a temporary `<a download>`. */
export function saveText(text: string, filename: string, mediaType: string) {
  const url = URL.createObjectURL(new Blob([text], { type: mediaType }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
}

export function fileNameFor(odcsId: string, label: string, format: DocFormat) {
  const base = trimChar(odcsId.replace(/[^A-Za-z0-9._-]+/g, "-"), "-");
  return `${base || "contract"}-${label}.${format === "json" ? "json" : "yaml"}`;
}

/** A fetch URL made absolute against this origin. */
export function absoluteUrl(path: string): string {
  return new URL(path, window.location.origin).toString();
}

export { shortPrincipal } from "@/components/PrincipalName";
