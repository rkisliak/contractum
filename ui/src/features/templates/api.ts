// Template reads and writes over the typed client (Phase 11, D-UI-11-03 to 05). Every query key
// starts with "templates", so one invalidation refreshes the lists, the page and its versions.
// Template documents are raw YAML or JSON: they are sent and read as text, never re-serialised.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import type {
  AvailableTemplate,
  Template,
  TemplateVersion,
  TemplateWrite,
} from "@/api/types";
import type { CursorPage } from "@/lib/useCursorList";

export const templateKeys = {
  all: ["templates"] as const,
  list: (folderId: string | null) =>
    ["templates", "list", { folderId }] as const,
  detail: (id: string) => ["templates", "detail", id] as const,
  document: (id: string) => ["templates", "document", id] as const,
  versions: (id: string) => ["templates", "versions", id] as const,
  versionDocument: (id: string, n: number) =>
    ["templates", "version-document", id, n] as const,
  available: (folderId: string) =>
    ["templates", "available", folderId] as const,
};

/** The media type a document's text is sent with: JSON when it starts with `{`, else YAML. */
export function mediaTypeOf(
  text: string,
): "application/json" | "application/yaml" {
  return text.trimStart().startsWith("{")
    ? "application/json"
    : "application/yaml";
}

/** A starting point for a new template (ODCS 3.1); the author replaces the placeholders. */
export const STARTER_TEMPLATE = `apiVersion: v3.1.0
kind: DataContract
id: urn:example:template:replace-me
version: 0.1.0
status: draft
name: Replace with the dataset name
description:
  purpose: Describe what this dataset holds and who uses it.
schema:
  - name: table_name
    physicalType: table
    properties:
      - name: id
        logicalType: string
        primaryKey: true
        required: true
`;

function useApi() {
  return useRouteContext({ from: "__root__" }).api;
}

const rawBody = (text: string) => ({
  body: text as never,
  bodySerializer: (body: unknown) => body as string,
  headers: { "Content-Type": mediaTypeOf(text) },
});

export function useTemplatePage() {
  const api = useApi();
  return async (
    folderId: string | null,
    cursor: string | null,
  ): Promise<CursorPage<Template>> => {
    const { data } = await api.GET("/api/v1/contracts/templates", {
      params: {
        query: {
          limit: 50,
          ...(folderId ? { folder_id: folderId } : {}),
          ...(cursor === null ? {} : { cursor }),
        },
      },
    });
    if (data === undefined) throw new Error("GET /templates had no body");
    return data;
  };
}

export function useAvailableTemplatePage() {
  const api = useApi();
  return async (
    folderId: string,
    cursor: string | null,
  ): Promise<CursorPage<AvailableTemplate>> => {
    const { data } = await api.GET(
      "/api/v1/contracts/folders/{folder_id}/available-templates",
      {
        params: {
          path: { folder_id: folderId },
          query: { limit: 50, ...(cursor === null ? {} : { cursor }) },
        },
      },
    );
    if (data === undefined)
      throw new Error("GET /available-templates had no body");
    return data;
  };
}

export function useTemplate(id: string) {
  const api = useApi();
  return useQuery({
    queryKey: templateKeys.detail(id),
    queryFn: async (): Promise<Template> => {
      const { data } = await api.GET(
        "/api/v1/contracts/templates/{template_id}",
        {
          params: { path: { template_id: id } },
        },
      );
      if (data === undefined)
        throw new Error("GET /templates/{id} had no body");
      return data;
    },
  });
}

export function useTemplateDocument(id: string, enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: templateKeys.document(id),
    enabled,
    queryFn: async (): Promise<string> => {
      const { data } = await api.GET(
        "/api/v1/contracts/templates/{template_id}/document",
        { params: { path: { template_id: id } }, parseAs: "text" },
      );
      return data ?? "";
    },
  });
}

export function useTemplateVersions(id: string) {
  const api = useApi();
  return useQuery({
    queryKey: templateKeys.versions(id),
    queryFn: async (): Promise<TemplateVersion[]> => {
      const { data } = await api.GET(
        "/api/v1/contracts/templates/{template_id}/versions",
        { params: { path: { template_id: id } } },
      );
      if (data === undefined) throw new Error("GET /versions had no body");
      return data.items;
    },
  });
}

export function useTemplateVersionDocument(
  id: string,
  versionNo: number | null,
) {
  const api = useApi();
  return useQuery({
    queryKey: templateKeys.versionDocument(id, versionNo ?? 0),
    enabled: versionNo !== null,
    queryFn: async (): Promise<string> => {
      const { data } = await api.GET(
        "/api/v1/contracts/templates/{template_id}/versions/{version_no}/document",
        {
          params: { path: { template_id: id, version_no: versionNo ?? 0 } },
          parseAs: "text",
        },
      );
      return data ?? "";
    },
  });
}

export function useTemplateWrites() {
  const api = useApi();
  const queryClient = useQueryClient();
  const settle = () =>
    queryClient.invalidateQueries({ queryKey: templateKeys.all });

  const create = useMutation({
    mutationFn: async (input: {
      folderId: string;
      name: string;
      document: string;
    }): Promise<TemplateWrite> => {
      const { data } = await api.POST(
        "/api/v1/contracts/folders/{folder_id}/templates",
        {
          params: {
            path: { folder_id: input.folderId },
            query: { name: input.name },
          },
          ...rawBody(input.document),
        },
      );
      if (data === undefined) throw new Error("POST /templates had no body");
      return data;
    },
    onSuccess: settle,
  });

  const saveDraft = useMutation({
    mutationFn: async (input: {
      templateId: string;
      document: string;
      name?: string;
    }): Promise<TemplateWrite> => {
      const { data } = await api.PUT(
        "/api/v1/contracts/templates/{template_id}/draft",
        {
          params: {
            path: { template_id: input.templateId },
            query: input.name ? { name: input.name } : {},
          },
          ...rawBody(input.document),
        },
      );
      if (data === undefined) throw new Error("PUT /draft had no body");
      return data;
    },
    onSuccess: settle,
  });

  const publish = useMutation({
    mutationFn: async (templateId: string) => {
      const { data } = await api.POST(
        "/api/v1/contracts/templates/{template_id}/publish",
        { params: { path: { template_id: templateId } } },
      );
      if (data === undefined) throw new Error("POST /publish had no body");
      return data;
    },
    onSuccess: settle,
  });

  const deprecate = useMutation({
    mutationFn: async (input: { templateId: string; versionNo: number }) => {
      const { data } = await api.POST(
        "/api/v1/contracts/templates/{template_id}/versions/{version_no}/deprecate",
        {
          params: {
            path: {
              template_id: input.templateId,
              version_no: input.versionNo,
            },
          },
        },
      );
      if (data === undefined) throw new Error("POST /deprecate had no body");
      return data;
    },
    onSuccess: settle,
  });

  return { create, saveDraft, publish, deprecate };
}
