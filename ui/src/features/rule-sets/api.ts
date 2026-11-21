// Rule-set reads and writes over the typed client (Phase 11, D-UI-11-02 and 06 to 12). Every
// query key starts with "rule-sets". Draft writes carry `If-Match: "r<draft_revision>"` (D-114);
// a 412 means someone else saved first and the page offers to reload the draft.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import type {
  AvailableRuleSet,
  RuleDef,
  RuleSet,
  RuleSetDraft,
  RuleSetVersion,
  RuleSetVersionSummary,
} from "@/api/types";

export const ruleSetKeys = {
  all: ["rule-sets"] as const,
  folder: (folderId: string) => ["rule-sets", "folder", folderId] as const,
  available: (folderId: string) =>
    ["rule-sets", "available", folderId] as const,
  availableOne: (folderId: string, id: string) =>
    ["rule-sets", "available-one", folderId, id] as const,
  draft: (id: string) => ["rule-sets", "draft", id] as const,
  versions: (id: string) => ["rule-sets", "versions", id] as const,
  version: (id: string, n: number, via: string | null) =>
    ["rule-sets", "version", id, n, via] as const,
};

export const etagOf = (revision: number) => `"r${String(revision)}"`;

export function useApi() {
  return useRouteContext({ from: "__root__" }).api;
}

export function useFolderRuleSets(folderId: string) {
  const api = useApi();
  return useQuery({
    queryKey: ruleSetKeys.folder(folderId),
    enabled: folderId !== "",
    queryFn: async (): Promise<RuleSet[]> => {
      const { data } = await api.GET(
        "/api/v1/validation/folders/{folder_id}/rule-sets",
        { params: { path: { folder_id: folderId } } },
      );
      if (data === undefined) throw new Error("GET /rule-sets had no body");
      return data.items;
    },
  });
}

export function useAvailableRuleSets(folderId: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: ruleSetKeys.available(folderId ?? ""),
    enabled: folderId !== null && folderId !== "",
    queryFn: async (): Promise<AvailableRuleSet[]> => {
      const { data } = await api.GET(
        "/api/v1/validation/folders/{folder_id}/available-rule-sets",
        { params: { path: { folder_id: folderId ?? "" } } },
      );
      if (data === undefined)
        throw new Error("GET /available-rule-sets had no body");
      return data.items;
    },
  });
}

/** One published rule set as seen from a folder (also the read-only view of platform sets). */
export function useAvailableRuleSet(
  folderId: string | null,
  id: string,
  enabled = true,
) {
  const api = useApi();
  return useQuery({
    queryKey: ruleSetKeys.availableOne(folderId ?? "", id),
    enabled: enabled && folderId !== null,
    queryFn: async (): Promise<AvailableRuleSet> => {
      const { data } = await api.GET(
        "/api/v1/validation/folders/{folder_id}/available-rule-sets/{rule_set_id}",
        { params: { path: { folder_id: folderId ?? "", rule_set_id: id } } },
      );
      if (data === undefined)
        throw new Error("GET /available-rule-sets/{id} had no body");
      return data;
    },
  });
}

export function useRuleSetDraft(id: string, enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: ruleSetKeys.draft(id),
    enabled,
    queryFn: async (): Promise<RuleSetDraft> => {
      const { data } = await api.GET(
        "/api/v1/validation/rule-sets/{rule_set_id}",
        {
          params: { path: { rule_set_id: id } },
        },
      );
      if (data === undefined)
        throw new Error("GET /rule-sets/{id} had no body");
      return data;
    },
  });
}

export function useRuleSetVersions(id: string, enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: ruleSetKeys.versions(id),
    enabled,
    queryFn: async (): Promise<RuleSetVersionSummary[]> => {
      const { data } = await api.GET(
        "/api/v1/validation/rule-sets/{rule_set_id}/versions",
        { params: { path: { rule_set_id: id } } },
      );
      if (data === undefined) throw new Error("GET /versions had no body");
      return data.items;
    },
  });
}

/** A version's rules: through the owner route, or through `via` folder's available view. */
export function useRuleSetVersion(
  id: string,
  versionNo: number | null,
  via: string | null,
) {
  const api = useApi();
  return useQuery({
    queryKey: ruleSetKeys.version(id, versionNo ?? 0, via),
    enabled: versionNo !== null,
    queryFn: async (): Promise<RuleSetVersion> => {
      const n = versionNo ?? 0;
      const { data } =
        via === null
          ? await api.GET(
              "/api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}",
              { params: { path: { rule_set_id: id, version_no: n } } },
            )
          : await api.GET(
              "/api/v1/validation/folders/{folder_id}/available-rule-sets/{rule_set_id}/versions/{version_no}",
              {
                params: {
                  path: { folder_id: via, rule_set_id: id, version_no: n },
                },
              },
            );
      if (data === undefined) throw new Error("GET /versions/{n} had no body");
      return data;
    },
  });
}

export function useRuleSetWrites(id: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const settle = () =>
    queryClient.invalidateQueries({ queryKey: ruleSetKeys.all });
  // Draft writes answer with the new draft: store it so the next If-Match is current at once.
  const storeDraft = (draft: RuleSetDraft) => {
    queryClient.setQueryData(ruleSetKeys.draft(id), {
      ...draft,
      check: null,
    });
    void queryClient.invalidateQueries({
      queryKey: ruleSetKeys.all,
      predicate: (query) => query.queryKey[1] !== "draft",
    });
  };

  const putRule = useMutation({
    mutationFn: async (input: {
      revision: number;
      rule: RuleDef;
    }): Promise<RuleSetDraft> => {
      const { data } = await api.PUT(
        "/api/v1/validation/rule-sets/{rule_set_id}/draft/rules/{rule_key}",
        {
          params: { path: { rule_set_id: id, rule_key: input.rule.key } },
          headers: { "If-Match": etagOf(input.revision) },
          body: input.rule as never,
        },
      );
      if (data === undefined) throw new Error("PUT rule had no body");
      return data;
    },
    onSuccess: storeDraft,
  });

  const deleteRule = useMutation({
    mutationFn: async (input: {
      revision: number;
      key: string;
    }): Promise<RuleSetDraft> => {
      const { data } = await api.DELETE(
        "/api/v1/validation/rule-sets/{rule_set_id}/draft/rules/{rule_key}",
        {
          params: { path: { rule_set_id: id, rule_key: input.key } },
          headers: { "If-Match": etagOf(input.revision) },
        },
      );
      if (data === undefined) throw new Error("DELETE rule had no body");
      return data;
    },
    onSuccess: storeDraft,
  });

  const putDraft = useMutation({
    mutationFn: async (input: {
      revision: number;
      draft: { description?: string | null; rules: RuleDef[] };
    }): Promise<RuleSetDraft> => {
      const { data } = await api.PUT(
        "/api/v1/validation/rule-sets/{rule_set_id}/draft",
        {
          params: { path: { rule_set_id: id } },
          headers: { "If-Match": etagOf(input.revision) },
          body: input.draft as never,
        },
      );
      if (data === undefined) throw new Error("PUT draft had no body");
      return data;
    },
    onSuccess: storeDraft,
  });

  const check = useMutation({
    mutationFn: async (): Promise<RuleSetDraft> => {
      const { data } = await api.POST(
        "/api/v1/validation/rule-sets/{rule_set_id}/draft/check",
        { params: { path: { rule_set_id: id } } },
      );
      if (data === undefined) throw new Error("POST check had no body");
      return data;
    },
  });

  const publish = useMutation({
    mutationFn: async (revision: number): Promise<RuleSetVersion> => {
      const { data } = await api.POST(
        "/api/v1/validation/rule-sets/{rule_set_id}/publish",
        {
          params: { path: { rule_set_id: id } },
          headers: { "If-Match": etagOf(revision) },
        },
      );
      if (data === undefined) throw new Error("POST publish had no body");
      return data;
    },
    onSuccess: settle,
  });

  const deprecate = useMutation({
    mutationFn: async (versionNo: number) => {
      await api.POST(
        "/api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}/deprecate",
        { params: { path: { rule_set_id: id, version_no: versionNo } } },
      );
    },
    onSuccess: settle,
  });

  const setMandatory = useMutation({
    mutationFn: async (mandatory: boolean) => {
      await api.PUT("/api/v1/validation/rule-sets/{rule_set_id}/mandatory", {
        params: { path: { rule_set_id: id } },
        body: { mandatory },
      });
    },
    onSuccess: settle,
  });

  /** Enforce a warning version now; platform sets go through the ROOT folder route (D-UI-11-12). */
  const enforceNow = useMutation({
    mutationFn: async (input: {
      versionNo: number;
      platformRootId?: string;
    }) => {
      if (input.platformRootId !== undefined) {
        await api.POST(
          "/api/v1/validation/folders/{folder_id}/platform-rule-sets/{rule_set_id}/versions/{version_no}/enforce-now",
          {
            params: {
              path: {
                folder_id: input.platformRootId,
                rule_set_id: id,
                version_no: input.versionNo,
              },
            },
          },
        );
        return;
      }
      await api.POST(
        "/api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}/enforce-now",
        { params: { path: { rule_set_id: id, version_no: input.versionNo } } },
      );
    },
    onSuccess: settle,
  });

  /** Pause or resume a platform set; only ROOT ruleset:mandate holders may (quick 261118-vpa). */
  const setPaused = useMutation({
    mutationFn: async (input: { platformRootId: string; paused: boolean }) => {
      await api.PUT(
        "/api/v1/validation/folders/{folder_id}/platform-rule-sets/{rule_set_id}/paused",
        {
          params: {
            path: { folder_id: input.platformRootId, rule_set_id: id },
          },
          body: { paused: input.paused },
        },
      );
    },
    onSuccess: settle,
  });

  /** Move a version's enforcement date to any timestamp; platform sets use the ROOT route. */
  const reschedule = useMutation({
    mutationFn: async (input: {
      versionNo: number;
      enforceAt: string;
      platformRootId?: string;
    }) => {
      if (input.platformRootId !== undefined) {
        await api.PUT(
          "/api/v1/validation/folders/{folder_id}/platform-rule-sets/{rule_set_id}/versions/{version_no}/enforce-at",
          {
            params: {
              path: {
                folder_id: input.platformRootId,
                rule_set_id: id,
                version_no: input.versionNo,
              },
            },
            body: { enforce_at: input.enforceAt },
          },
        );
        return;
      }
      await api.PUT(
        "/api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}/enforce-at",
        {
          params: { path: { rule_set_id: id, version_no: input.versionNo } },
          body: { enforce_at: input.enforceAt },
        },
      );
    },
    onSuccess: settle,
  });

  const remove = useMutation({
    mutationFn: async () => {
      await api.DELETE("/api/v1/validation/rule-sets/{rule_set_id}", {
        params: { path: { rule_set_id: id } },
      });
    },
    onSuccess: settle,
  });

  return {
    putRule,
    deleteRule,
    putDraft,
    check,
    publish,
    deprecate,
    setMandatory,
    enforceNow,
    setPaused,
    reschedule,
    remove,
  };
}

export function useCreateRuleSet() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      folderId: string;
      name: string;
      description: string | null;
    }): Promise<RuleSet> => {
      const { data } = await api.POST(
        "/api/v1/validation/folders/{folder_id}/rule-sets",
        {
          params: { path: { folder_id: input.folderId } },
          body: { name: input.name, description: input.description },
        },
      );
      if (data === undefined) throw new Error("POST /rule-sets had no body");
      return data;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ruleSetKeys.all }),
  });
}
