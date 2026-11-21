// Admin > Settings data (quick 261120-0yc). Every key sits under ["settings", ...]; a successful
// save settles the whole prefix, so the public banner and the Register pre-fill refresh too. The
// reads are enabled only for a caller that holds `settings:read` (the page renders NotAvailable
// otherwise), so a non-admin never makes a settings request. The API re-checks every call.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  EntraOverview,
  EntraTest,
  NotificationPreview,
  NotificationPreviewIn,
  SettingsAbout,
  SettingsImport,
  SettingsOut,
  SettingsPublic,
  SettingsSection,
  TestEmail,
} from "@/api/types";
import { hasGlobal, useMe } from "@/features/access/useMe";
import { useApi } from "@/features/admin/shared";

export const settingsKeys = {
  all: ["settings"] as const,
  list: ["settings", "list"] as const,
  public: ["settings", "public"] as const,
  overview: ["settings", "entra-overview"] as const,
  about: ["settings", "about"] as const,
};

/** True once `/me` says the caller may read settings. */
export function useCanReadSettings(): boolean {
  const me = useMe();
  return hasGlobal(me.data, "settings:read");
}

export function useSettings() {
  const api = useApi();
  const enabled = useCanReadSettings();
  return useQuery({
    queryKey: settingsKeys.list,
    enabled,
    queryFn: async (): Promise<SettingsOut> => {
      const { data } = await api.GET("/api/v1/identity/settings");
      if (data === undefined) throw new Error("GET /settings had no body");
      return data;
    },
  });
}

/** The banner and the Register pre-fill values: any signed-in user, a failure shows nothing. */
export function usePublicSettings() {
  const api = useApi();
  const me = useMe();
  return useQuery({
    queryKey: settingsKeys.public,
    enabled: me.data !== undefined,
    staleTime: 60_000,
    retry: false,
    queryFn: async (): Promise<SettingsPublic> => {
      const { data } = await api.GET("/api/v1/identity/settings/public");
      if (data === undefined) throw new Error("GET /settings/public no body");
      return data;
    },
  });
}

export function useSaveSection() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      section: string;
      values: Record<string, unknown>;
      etag: string;
    }): Promise<SettingsSection> => {
      const { data } = await api.PUT("/api/v1/identity/settings/{section}", {
        params: {
          path: { section: input.section },
          header: { "if-match": input.etag },
        },
        body: input.values,
      });
      if (data === undefined) throw new Error("PUT /settings had no body");
      return data;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: settingsKeys.all }),
  });
}

export function useEntraOverview() {
  const api = useApi();
  const enabled = useCanReadSettings();
  return useQuery({
    queryKey: settingsKeys.overview,
    enabled,
    queryFn: async (): Promise<EntraOverview> => {
      const { data } = await api.GET(
        "/api/v1/identity/settings/entra/overview",
      );
      if (data === undefined) throw new Error("overview had no body");
      return data;
    },
  });
}

export function useEntraTest() {
  const api = useApi();
  return useMutation({
    mutationFn: async (): Promise<EntraTest> => {
      const { data } = await api.POST("/api/v1/identity/settings/entra/test");
      if (data === undefined) throw new Error("test had no body");
      return data;
    },
  });
}

export function useAbout() {
  const api = useApi();
  const enabled = useCanReadSettings();
  return useQuery({
    queryKey: settingsKeys.about,
    enabled,
    queryFn: async (): Promise<SettingsAbout> => {
      const { data } = await api.GET("/api/v1/identity/settings/about");
      if (data === undefined) throw new Error("about had no body");
      return data;
    },
  });
}

/** The file name from `Content-Disposition`, or a dated fallback. */
export function exportFileName(header: string | null): string {
  const found = /filename="([A-Za-z0-9._-]{1,100})"/.exec(header ?? "");
  return found?.[1] ?? "contractum-settings.json";
}

export function useExportSettings() {
  const api = useApi();
  return useMutation({
    mutationFn: async (): Promise<void> => {
      const { data, response } = await api.GET(
        "/api/v1/identity/settings/export",
        { parseAs: "blob" },
      );
      const url = URL.createObjectURL(data as Blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = exportFileName(
        response.headers.get("Content-Disposition"),
      );
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    },
  });
}

/** The import body is the file's own text: the API parses it with its depth-capped loader. */
export function useImportSettings() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      text: string;
      apply: boolean;
      etag?: string;
    }): Promise<SettingsImport> => {
      const { data } = await api.POST("/api/v1/identity/settings/import", {
        params: {
          query: { dry_run: !input.apply },
          header: input.etag === undefined ? {} : { "if-match": input.etag },
        },
        headers: { "Content-Type": "application/json" },
        body: {},
        bodySerializer: () => input.text,
      });
      if (data === undefined) throw new Error("import had no body");
      return data;
    },
    onSuccess: (_data, input) =>
      input.apply
        ? queryClient.invalidateQueries({ queryKey: settingsKeys.all })
        : undefined,
  });
}

/** Renders a template with the fixed sample, server side (nothing is saved or audited). */
export function usePreviewTemplate() {
  const api = useApi();
  return useMutation({
    mutationFn: async (
      input: NotificationPreviewIn,
    ): Promise<NotificationPreview> => {
      const { data } = await api.POST(
        "/api/v1/identity/settings/notifications/preview",
        { body: input },
      );
      if (data === undefined) throw new Error("preview had no body");
      return data;
    },
  });
}

/** Sends one fixed test message through the mail service with the admin's own sign-in. */
export function useSendTestEmail() {
  const api = useApi();
  return useMutation({
    mutationFn: async (to: string): Promise<TestEmail> => {
      const { data } = await api.POST("/api/v1/contracts/settings/test-email", {
        body: { to },
      });
      if (data === undefined) throw new Error("test email had no body");
      return data;
    },
  });
}
