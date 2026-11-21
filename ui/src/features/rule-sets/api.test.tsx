// The rule-set data hooks on their own: a 2xx answer without a body is a failure (never an empty
// list or a silent success), and the If-Match and platform-route variants reach the right URLs.
import { act, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderDataHook } from "@/test/governance-hooks";
import { http, HttpResponse, server } from "@/test/msw";
import {
  useAvailableRuleSet,
  useAvailableRuleSets,
  useCreateRuleSet,
  useFolderRuleSets,
  useRuleSetDraft,
  useRuleSetVersion,
  useRuleSetVersions,
  useRuleSetWrites,
} from "./api";

const V = "*/api/v1/validation";
const empty = () => new HttpResponse(null, { status: 204 });

/** One read: its name, the URL that answers 204, the hook, and the message it must fail with. */
type BodilessRead = [string, string, () => { error: Error | null }, string];

describe("rule-set reads without a body", () => {
  it.each<BodilessRead>([
    [
      "the folder list",
      `${V}/folders/acme-id/rule-sets`,
      () => useFolderRuleSets("acme-id"),
      "GET /rule-sets had no body",
    ],
    [
      "the available list",
      `${V}/folders/acme-id/available-rule-sets`,
      () => useAvailableRuleSets("acme-id"),
      "GET /available-rule-sets had no body",
    ],
    [
      "one available set",
      `${V}/folders/acme-id/available-rule-sets/rs-1`,
      () => useAvailableRuleSet("acme-id", "rs-1"),
      "GET /available-rule-sets/{id} had no body",
    ],
    [
      "the draft",
      `${V}/rule-sets/rs-1`,
      () => useRuleSetDraft("rs-1"),
      "GET /rule-sets/{id} had no body",
    ],
    [
      "the versions",
      `${V}/rule-sets/rs-1/versions`,
      () => useRuleSetVersions("rs-1", true),
      "GET /versions had no body",
    ],
    [
      "a version through the owner route",
      `${V}/rule-sets/rs-1/versions/2`,
      () => useRuleSetVersion("rs-1", 2, null),
      "GET /versions/{n} had no body",
    ],
  ])("fails %s", async (_name, url, useHook, message) => {
    server.use(http.get(url, empty));
    const { result } = await renderDataHook(useHook);
    await waitFor(() => {
      expect(result.current.error?.message).toBe(message);
    });
  });
});

describe("rule-set writes without a body", () => {
  const rule = {
    key: "has-owner",
    description: null,
    target: "$",
    condition: "has(this.owner)",
    severity: "error" as const,
    message: "Name an owner.",
    match: "each" as const,
    on_empty: "pass" as const,
    fixtures: { pass: [], fail: [] },
  };

  it("rejects every draft write and the publish that answer 204", async () => {
    const ifMatch: (string | null)[] = [];
    const record = ({ request }: { request: Request }) => {
      ifMatch.push(request.headers.get("If-Match"));
      return empty();
    };
    server.use(
      http.put(`${V}/rule-sets/rs-1/draft/rules/has-owner`, record),
      http.delete(`${V}/rule-sets/rs-1/draft/rules/has-owner`, record),
      http.put(`${V}/rule-sets/rs-1/draft`, record),
      http.post(`${V}/rule-sets/rs-1/draft/check`, empty),
      http.post(`${V}/rule-sets/rs-1/publish`, record),
    );
    const { result } = await renderDataHook(() => useRuleSetWrites("rs-1"));
    await act(async () => {
      await expect(
        result.current.putRule.mutateAsync({ revision: 4, rule }),
      ).rejects.toThrow("PUT rule had no body");
      await expect(
        result.current.deleteRule.mutateAsync({ revision: 5, key: rule.key }),
      ).rejects.toThrow("DELETE rule had no body");
      await expect(
        result.current.putDraft.mutateAsync({
          revision: 6,
          draft: { rules: [rule] },
        }),
      ).rejects.toThrow("PUT draft had no body");
      await expect(result.current.check.mutateAsync()).rejects.toThrow(
        "POST check had no body",
      );
      await expect(result.current.publish.mutateAsync(7)).rejects.toThrow(
        "POST publish had no body",
      );
    });
    expect(ifMatch).toEqual(['"r4"', '"r5"', '"r6"', '"r7"']);
  });

  it("rejects a create that answers 204", async () => {
    server.use(http.post(`${V}/folders/acme-id/rule-sets`, empty));
    const { result } = await renderDataHook(() => useCreateRuleSet());
    await act(async () => {
      await expect(
        result.current.mutateAsync({
          folderId: "acme-id",
          name: "acme-new",
          description: null,
        }),
      ).rejects.toThrow("POST /rule-sets had no body");
    });
  });
});
