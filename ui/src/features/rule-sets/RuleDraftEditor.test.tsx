// The draft editor on its own, for the refusals the screen journeys do not reach: a conflict whose
// server copy cannot be loaded or no longer holds the rule, and a JSON replace refused with only a
// title.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { describe, expect, it, vi } from "vitest";
import type { RuleDef, RuleSetDraft } from "@/api/types";
import { renderWithApi } from "@/test/governance-hooks";
import { http, HttpResponse, server } from "@/test/msw";
import { RuleDraftEditor } from "./RuleDraftEditor";

const V = "*/api/v1/validation";

const alpha: RuleDef = {
  key: "alpha-rule",
  description: null,
  target: "$",
  condition: "has(this.description)",
  severity: "error",
  message: "Add a description.",
  match: "each",
  on_empty: "pass",
  fixtures: { pass: [{ document: { description: {} } }], fail: [] },
};

function draft(rules: RuleDef[], revision = 3): RuleSetDraft {
  return {
    rule_set: {
      id: "rs-1",
      scope: "folder",
      folder_id: "acme-id",
      folder_kind: "organization",
      name: "acme-baseline",
      description: "Baseline rules",
      publish_counter: 1,
      draft_revision: revision,
      mandatory: false,
      mandatory_set_at: null,
      mandatory_set_by: null,
      created_by: "local:admin",
      created_at: "2026-11-01T10:00:00Z",
      updated_by: "local:admin",
      updated_at: "2026-11-02T10:00:00Z",
    },
    rules,
    check: null,
  };
}

function refused(status: number, body: Record<string, unknown> = {}) {
  return HttpResponse.json(
    { type: "urn:dc:problem:refused", title: "Draft refused", status, ...body },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

function mount(handlers: RequestHandler[]) {
  server.use(
    http.get("*/api/v1/identity/me/capabilities", () =>
      HttpResponse.json({
        folder_id: "acme-id",
        roles: ["org_admin"],
        permissions: ["ruleset:read", "ruleset:update", "ruleset:publish"],
        inheritance_blocked: false,
        grantable_roles: [],
      }),
    ),
    ...handlers,
  );
  const onRefetch = vi.fn();
  renderWithApi(
    <RuleDraftEditor draft={draft([alpha])} onRefetch={onRefetch} />,
  );
  return { onRefetch };
}

describe("rule draft editor conflicts", () => {
  it("says the server copy could not be loaded when it comes back empty", async () => {
    const user = userEvent.setup();
    const { onRefetch } = mount([
      http.put(`${V}/rule-sets/rs-1/draft/rules/alpha-rule`, () =>
        refused(412),
      ),
      http.get(
        `${V}/rule-sets/rs-1`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    ]);
    await user.click(await screen.findByRole("button", { name: "Save rule" }));
    expect(
      await screen.findByText(
        "The server copy could not be loaded for comparison.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Reload draft" }));
    expect(onRefetch).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Someone else changed this draft")).toBeNull();
  });

  it("says when the rule is gone from the server draft", async () => {
    const user = userEvent.setup();
    mount([
      http.put(`${V}/rule-sets/rs-1/draft/rules/alpha-rule`, () =>
        refused(412),
      ),
      http.get(`${V}/rule-sets/rs-1`, () => HttpResponse.json(draft([], 8))),
    ]);
    await user.click(await screen.findByRole("button", { name: "Save rule" }));
    expect(
      await screen.findByLabelText("Server rule alpha-rule, revision 8"),
    ).toHaveTextContent("Rule alpha-rule is not in the server draft.");
  });
});

describe("rule draft editor JSON replace", () => {
  it("shows the problem title when the refusal has no detail and no field errors", async () => {
    const user = userEvent.setup();
    mount([http.put(`${V}/rule-sets/rs-1/draft`, () => refused(400))]);
    await user.click(
      await screen.findByRole("button", { name: "Edit draft as JSON" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Edit draft as JSON",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Replace draft" }),
    );
    await waitFor(() => {
      expect(within(dialog).getByText("Draft refused")).toBeVisible();
    });
  });
});
