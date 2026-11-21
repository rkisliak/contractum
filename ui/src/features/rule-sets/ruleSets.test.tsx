// Rule sets through the real router: the folder lists, the draft editor (save a rule with
// If-Match, compile errors from the server check, publish only after a clean check), rollout
// (enforce now) and the read-only view of an inherited platform set.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  AvailableRuleSet,
  RuleDef,
  RuleSet,
  RuleSetDraft,
} from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder, ROOT_ID } from "@/test/renderApp";

vi.setConfig({ testTimeout: 20000 });

afterEach(() => {
  tokenStore.clear();
});

const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const folders = [rootFolder, acme];
const PERMISSIONS = [
  "ruleset:create",
  "ruleset:read",
  "ruleset:update",
  "ruleset:publish",
  "ruleset:deprecate",
  "ruleset:mandate",
];

function ruleSet(overrides: Partial<RuleSet> = {}): RuleSet {
  return {
    id: "rs-1",
    scope: "folder",
    folder_id: "acme-id",
    folder_kind: "organization",
    name: "acme-baseline",
    description: "Baseline rules",
    publish_counter: 0,
    draft_revision: 3,
    mandatory: false,
    mandatory_set_at: null,
    mandatory_set_by: null,
    created_by: "local:admin",
    created_at: "2026-11-01T10:00:00Z",
    updated_by: "local:admin",
    updated_at: "2026-11-02T10:00:00Z",
    ...overrides,
  };
}

const rule: RuleDef = {
  key: "require-description",
  description: null,
  target: "$",
  condition: "has(this.description)",
  severity: "error",
  message: "Add a description.",
  match: "each",
  on_empty: "pass",
  fixtures: {
    pass: [{ document: { description: {} } }],
    fail: [{ document: {} }],
  },
};

function draft(overrides: Partial<RuleSetDraft> = {}): RuleSetDraft {
  return { rule_set: ruleSet(), rules: [rule], check: null, ...overrides };
}

function available(
  overrides: Partial<AvailableRuleSet> = {},
): AvailableRuleSet {
  return {
    id: "rs-1",
    scope: "folder",
    folder_id: "acme-id",
    folder_kind: "organization",
    name: "acme-baseline",
    description: null,
    latest_version: 1,
    latest_active_version: 1,
    mandatory: true,
    paused: false,
    paused_at: null,
    versions: [
      {
        version_no: 1,
        content_sha256: "x",
        rule_keys: ["require-description"],
        deprecated_at: null,
        enforce_at: "2026-11-29T00:00:00Z",
        published_at: "2026-11-15T00:00:00Z",
        published_by: "local:admin",
      },
    ],
    resolved: [
      {
        version_no: 1,
        mode: "warn",
        enforce_at: "2026-11-29T00:00:00Z",
        paused: false,
      },
    ],
    ...overrides,
  };
}

const notFound = () =>
  HttpResponse.json(
    { type: "urn:dc:problem:not-found", title: "Not Found", status: 404 },
    { status: 404, headers: { "Content-Type": "application/problem+json" } },
  );

describe("folder rule sets tab", () => {
  it("lists rule sets defined here and inherited ones", async () => {
    renderApp("/folders/acme-id/rule-sets", {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/validation/folders/acme-id/rule-sets", () =>
          HttpResponse.json({ items: [ruleSet()] }),
        ),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets",
          () =>
            HttpResponse.json({
              items: [
                available({
                  id: "plat",
                  name: "odcs-structural",
                  scope: "platform",
                  folder_id: null,
                  folder_kind: null,
                  resolved: [
                    {
                      version_no: 1,
                      mode: "enforce",
                      enforce_at: null,
                      paused: false,
                    },
                  ],
                }),
              ],
            }),
        ),
      ],
    });
    expect(
      await screen.findByRole("link", { name: "acme-baseline" }),
    ).toHaveAttribute("href", "/rule-sets/rs-1");
    const inherited = await screen.findByRole("link", {
      name: "odcs-structural",
    });
    expect(inherited).toHaveAttribute("href", "/rule-sets/plat?via=acme-id");
    const row = inherited.closest("tr") as HTMLElement;
    // The owning folder is body text, not code.
    const owner = within(row).getAllByRole("cell")[0];
    expect(owner).toHaveTextContent("Platform");
    expect(owner?.querySelector("code")).toBeNull();
    expect(within(row).getByText("Enforced")).toBeVisible();
    expect(
      await screen.findByRole("button", { name: "Create rule set" }),
    ).toBeVisible();
  });
});

describe("folder rule sets tab without ruleset:read", () => {
  it("never requests Defined here and shows only the inherited list", async () => {
    let definedRequested = false;
    renderApp("/folders/acme-id/rule-sets", {
      folders,
      permissions: ["ruleset:read-published"],
      handlers: [
        http.get("*/api/v1/validation/folders/acme-id/rule-sets", () => {
          definedRequested = true;
          return HttpResponse.json({ items: [] }, { status: 404 });
        }),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets",
          () => HttpResponse.json({ items: [] }),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", {
        name: "Available here (inherited)",
      }),
    ).toBeVisible();
    expect(await screen.findByText("No rule sets apply here")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Defined here" })).toBeNull();
    expect(screen.queryByText("Request ID")).toBeNull();
    expect(definedRequested).toBe(false);
  });
});

describe("rule set draft editor", () => {
  it("saves a rule with If-Match, shows compile errors, and publishes after a clean check", async () => {
    const user = userEvent.setup();
    let ifMatch: string | null = null;
    let publishIfMatch: string | null = null;
    let revision = 3;
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/validation/rule-sets/rs-1", () =>
          HttpResponse.json(
            draft({ rule_set: ruleSet({ draft_revision: revision }) }),
          ),
        ),
        http.get("*/api/v1/validation/rule-sets/rs-1/versions", () =>
          HttpResponse.json({ items: [] }),
        ),
        http.put(
          "*/api/v1/validation/rule-sets/rs-1/draft/rules/require-description",
          async ({ request }) => {
            ifMatch = request.headers.get("If-Match");
            const body = (await request.json()) as RuleDef;
            revision = 4;
            return HttpResponse.json(
              draft({
                rule_set: ruleSet({ draft_revision: 4 }),
                rules: [body],
                check: {
                  ok: false,
                  rule_set_problems: [],
                  rules: [
                    {
                      rule_key: "require-description",
                      ok: false,
                      problems: [
                        {
                          rule_key: "require-description",
                          code: "compile_error",
                          detail: "Syntax error: unexpected single '&'",
                        },
                      ],
                    },
                  ],
                },
              }),
            );
          },
        ),
        http.post("*/api/v1/validation/rule-sets/rs-1/draft/check", () =>
          HttpResponse.json(
            draft({
              rule_set: ruleSet({ draft_revision: revision }),
              check: {
                ok: true,
                rule_set_problems: [],
                rules: [
                  { rule_key: "require-description", ok: true, problems: [] },
                ],
              },
            }),
          ),
        ),
        http.post(
          "*/api/v1/validation/rule-sets/rs-1/publish",
          ({ request }) => {
            publishIfMatch = request.headers.get("If-Match");
            return HttpResponse.json(
              {
                id: "v1",
                rule_set_id: "rs-1",
                version_no: 1,
                dsl_version: "cel-2",
                rules: [rule],
                rule_keys: [rule.key],
                content_sha256: "x",
                enforce_at: null,
                deprecated_at: null,
                deprecated_by: null,
                published_at: "2026-11-15T00:00:00Z",
                published_by: "local:admin",
              },
              { status: 201 },
            );
          },
        ),
      ],
    });

    const condition = await screen.findByLabelText("Condition (CEL)");
    expect(condition).toHaveValue("has(this.description)");
    const publish = await screen.findByRole("button", {
      name: "Publish version 1",
    });
    expect(publish).toBeDisabled();

    await user.clear(condition);
    await user.type(condition, "has(this.description) &&& x");
    await user.click(screen.getByRole("button", { name: "Save rule" }));

    expect(
      await screen.findByText("The condition does not compile"),
    ).toBeVisible();
    expect(ifMatch).toBe('"r3"');
    expect(screen.getByText("Has problems")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Check draft" }));
    expect(
      await screen.findByText(
        "The draft passes its check. You can publish it.",
      ),
    ).toBeVisible();
    expect(screen.getByText("Check passed")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Publish version 1" }));
    const confirm = await screen.findByRole("alertdialog", {
      name: "Publish version 1 of acme-baseline?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Publish rule set" }),
    );
    expect(await screen.findByText("Version 1 is published.")).toBeVisible();
    expect(publishIfMatch).toBe('"r4"');
  });

  it("offers to reload the draft after a 412", async () => {
    const user = userEvent.setup();
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/validation/rule-sets/rs-1", () =>
          HttpResponse.json(draft()),
        ),
        http.get("*/api/v1/validation/rule-sets/rs-1/versions", () =>
          HttpResponse.json({ items: [] }),
        ),
        http.put(
          "*/api/v1/validation/rule-sets/rs-1/draft/rules/require-description",
          () =>
            HttpResponse.json(
              {
                type: "urn:dc:problem:precondition-failed",
                title: "Precondition Failed",
                status: 412,
              },
              {
                status: 412,
                headers: { "Content-Type": "application/problem+json" },
              },
            ),
        ),
      ],
    });
    await user.click(await screen.findByRole("button", { name: "Save rule" }));
    expect(
      await screen.findByText("Someone else changed this draft"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Reload draft" })).toBeVisible();
  });
});

describe("rule set rollout", () => {
  it("shows the warn date in the header and enforces a warning version now", async () => {
    const user = userEvent.setup();
    let enforced = 0;
    renderApp("/rule-sets/rs-1?tab=rollout", {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/validation/rule-sets/rs-1", () =>
          HttpResponse.json(
            draft({
              rule_set: ruleSet({ publish_counter: 1, mandatory: true }),
            }),
          ),
        ),
        http.get("*/api/v1/validation/rule-sets/rs-1/versions", () =>
          HttpResponse.json({ items: available().versions }),
        ),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets/rs-1",
          () => HttpResponse.json(available()),
        ),
        http.post(
          "*/api/v1/validation/rule-sets/rs-1/versions/1/enforce-now",
          () => {
            enforced += 1;
            return HttpResponse.json({});
          },
        ),
      ],
    });
    expect(await screen.findByText("Warn until 29 Nov 2026")).toBeVisible();
    expect(
      screen.getByRole("checkbox", {
        name: "Mandatory for acme and everything below",
      }),
    ).toBeChecked();
    await user.click(
      await screen.findByRole("button", { name: "Enforce version 1 now" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Enforce version 1 now?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Enforce now" }),
    );
    await waitFor(() => {
      expect(enforced).toBe(1);
    });
  });

  it("enforces a platform set through the ROOT folder route", async () => {
    const user = userEvent.setup();
    let path = "";
    renderApp(`/rule-sets/plat?via=acme-id&tab=rollout`, {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/validation/rule-sets/plat", notFound),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets/plat",
          () =>
            HttpResponse.json(
              available({
                id: "plat",
                name: "odcs-consistency",
                scope: "platform",
                folder_id: null,
                folder_kind: null,
              }),
            ),
        ),
        http.post(
          "*/api/v1/validation/folders/:folder/platform-rule-sets/plat/versions/1/enforce-now",
          ({ request }) => {
            path = new URL(request.url).pathname;
            return HttpResponse.json({});
          },
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "odcs-consistency",
      }),
    ).toBeVisible();
    expect(screen.getByText("Read only")).toBeVisible();
    expect(screen.queryByRole("tab", { name: "Draft" })).toBeNull();
    await user.click(
      await screen.findByRole("button", { name: "Enforce version 1 now" }),
    );
    const confirm = await screen.findByRole("alertdialog");
    await user.click(
      within(confirm).getByRole("button", { name: "Enforce now" }),
    );
    await waitFor(() => {
      expect(path).toBe(
        `/api/v1/validation/folders/${ROOT_ID}/platform-rule-sets/plat/versions/1/enforce-now`,
      );
    });
  });
});

describe("rule set pause and reschedule controls", () => {
  const platform = (overrides: Partial<AvailableRuleSet> = {}) =>
    available({
      id: "plat",
      name: "odcs-consistency",
      scope: "platform",
      folder_id: null,
      folder_kind: null,
      ...overrides,
    });
  const PLATFORM_URL =
    "*/api/v1/validation/folders/:folder/platform-rule-sets/plat";
  const problem409 = () =>
    HttpResponse.json(
      {
        type: "urn:dc:problem:rule-set-not-mandatory",
        title: "Conflict",
        status: 409,
        detail: "The version has no enforcement date.",
      },
      {
        status: 409,
        headers: { "Content-Type": "application/problem+json" },
      },
    );

  it("pauses and resumes a platform set through the ROOT route and shows the Paused badge", async () => {
    const user = userEvent.setup();
    let paused = false;
    const calls: { path: string; body: unknown }[] = [];
    renderApp(`/rule-sets/plat?via=acme-id&tab=rollout`, {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/validation/rule-sets/plat", notFound),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets/plat",
          () =>
            HttpResponse.json(
              platform(
                paused
                  ? {
                      paused: true,
                      paused_at: "2026-11-18T10:00:00Z",
                      resolved: [
                        {
                          version_no: 1,
                          mode: "warn",
                          enforce_at: "2026-11-29T00:00:00Z",
                          paused: true,
                        },
                      ],
                    }
                  : {},
              ),
            ),
        ),
        http.put(`${PLATFORM_URL}/paused`, async ({ request }) => {
          const body = (await request.json()) as { paused: boolean };
          calls.push({ path: new URL(request.url).pathname, body });
          paused = body.paused;
          return HttpResponse.json({});
        }),
      ],
    });
    expect(await screen.findByText("Warn until 29 Nov 2026")).toBeVisible();
    expect(
      screen.getByText(/Added by a platform update on 15 Nov 2026/),
    ).toBeVisible();
    await user.click(await screen.findByRole("button", { name: "Pause" }));
    const pauseDialog = await screen.findByRole("alertdialog", {
      name: "Pause odcs-consistency?",
    });
    expect(pauseDialog).toHaveTextContent(
      "Its rules keep running and report findings",
    );
    expect(pauseDialog).toHaveTextContent("won't block publishing");
    await user.click(
      within(pauseDialog).getByRole("button", {
        name: "Pause odcs-consistency",
      }),
    );
    await waitFor(() => {
      expect(calls).toEqual([
        {
          path: `/api/v1/validation/folders/${ROOT_ID}/platform-rule-sets/plat/paused`,
          body: { paused: true },
        },
      ]);
    });
    expect(await screen.findByRole("button", { name: "Resume" })).toBeVisible();
    // the header badge and the schedule row both read Paused
    expect((await screen.findAllByText("Paused")).length).toBeGreaterThan(1);
    expect(
      screen.getByText(/Paused on 18 Nov 2026: its rules keep running/),
    ).toBeVisible();
    expect(screen.getByText("29 Nov 2026 UTC once resumed")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Enforce version 1 now" }),
    ).toBeNull();

    await user.click(screen.getByRole("button", { name: "Resume" }));
    const resumeDialog = await screen.findByRole("alertdialog", {
      name: "Resume odcs-consistency?",
    });
    await user.click(
      within(resumeDialog).getByRole("button", {
        name: "Resume odcs-consistency",
      }),
    );
    await waitFor(() => {
      expect(calls[1]?.body).toEqual({ paused: false });
    });
    expect(await screen.findByRole("button", { name: "Pause" })).toBeVisible();
    expect(await screen.findByText("Warn until 29 Nov 2026")).toBeVisible();
  });

  it("changes the date of a platform version through the ROOT route", async () => {
    const user = userEvent.setup();
    const calls: { path: string; body: unknown }[] = [];
    renderApp(`/rule-sets/plat?via=acme-id&tab=rollout`, {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/validation/rule-sets/plat", notFound),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets/plat",
          () => HttpResponse.json(platform()),
        ),
        http.put(
          `${PLATFORM_URL}/versions/1/enforce-at`,
          async ({ request }) => {
            calls.push({
              path: new URL(request.url).pathname,
              body: await request.json(),
            });
            return HttpResponse.json({});
          },
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Change date… version 1" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Change the enforcement date of version 1?",
    });
    const field = within(dialog).getByLabelText("Blocks publishes from (UTC)");
    expect(field).toHaveValue("2026-11-29");
    expect(dialog).toHaveTextContent("Any date.");
    fireEvent.change(field, { target: { value: "2027-01-31" } });
    await user.click(within(dialog).getByRole("button", { name: "Save date" }));
    await waitFor(() => {
      expect(calls).toEqual([
        {
          path: `/api/v1/validation/folders/${ROOT_ID}/platform-rule-sets/plat/versions/1/enforce-at`,
          body: { enforce_at: "2027-01-31T00:00:00Z" },
        },
      ]);
    });
  });

  it("changes the date of a folder set, sends nothing for an empty date, and keeps a 409 in the dialog", async () => {
    const user = userEvent.setup();
    const calls: unknown[] = [];
    let refuse = true;
    renderApp("/rule-sets/rs-1?tab=rollout", {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/validation/rule-sets/rs-1", () =>
          HttpResponse.json(
            draft({
              rule_set: ruleSet({ publish_counter: 1, mandatory: true }),
            }),
          ),
        ),
        http.get("*/api/v1/validation/rule-sets/rs-1/versions", () =>
          HttpResponse.json({ items: available().versions }),
        ),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets/rs-1",
          () => HttpResponse.json(available()),
        ),
        http.put(
          "*/api/v1/validation/rule-sets/rs-1/versions/1/enforce-at",
          async ({ request }) => {
            calls.push(await request.json());
            if (refuse) return problem409();
            return HttpResponse.json({});
          },
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Change date… version 1" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Change the enforcement date of version 1?",
    });
    const field = within(dialog).getByLabelText("Blocks publishes from (UTC)");
    fireEvent.change(field, { target: { value: "" } });
    await user.click(within(dialog).getByRole("button", { name: "Save date" }));
    expect(await within(dialog).findByText("Choose a date.")).toBeVisible();
    expect(calls).toEqual([]);

    fireEvent.change(field, { target: { value: "2026-12-24" } });
    await user.click(within(dialog).getByRole("button", { name: "Save date" }));
    await waitFor(() => {
      expect(calls).toEqual([{ enforce_at: "2026-12-24T00:00:00Z" }]);
    });
    // the refusal stays in the open dialog
    expect(
      await within(dialog).findByText(/no enforcement date/i),
    ).toBeVisible();
    expect(
      screen.getByRole("alertdialog", {
        name: "Change the enforcement date of version 1?",
      }),
    ).toBeVisible();

    refuse = false;
    await user.click(within(dialog).getByRole("button", { name: "Save date" }));
    await waitFor(() => {
      expect(calls).toHaveLength(2);
    });
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    // an enforced warning row still offers Enforce now beside Change date
    expect(
      screen.getByRole("button", { name: "Enforce version 1 now" }),
    ).toBeVisible();
  });

  it("shows none of the controls to a caller without ruleset:mandate", async () => {
    const readOnly = PERMISSIONS.filter((p) => p !== "ruleset:mandate");
    renderApp(`/rule-sets/plat?via=acme-id&tab=rollout`, {
      folders,
      permissions: readOnly,
      handlers: [
        http.get("*/api/v1/validation/rule-sets/plat", notFound),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets/plat",
          () => HttpResponse.json(platform()),
        ),
      ],
    });
    expect(await screen.findByText("Warn until 29 Nov 2026")).toBeVisible();
    for (const name of [
      /^Pause/,
      /^Resume/,
      /Change date/,
      /Enforce version/,
    ]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("shows none of the controls on a platform set to a Policy Manager below ROOT", async () => {
    renderApp(`/rule-sets/plat?via=acme-id&tab=rollout`, {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        // ruleset:mandate is held at Acme only, never at ROOT
        http.get("*/api/v1/identity/me/capabilities", ({ request }) => {
          const folder = new URL(request.url).searchParams.get("folder_id");
          return HttpResponse.json({
            folder_id: folder,
            roles: ["policy_manager"],
            permissions:
              folder === "acme-id"
                ? PERMISSIONS
                : PERMISSIONS.filter((p) => p !== "ruleset:mandate"),
            inheritance_blocked: false,
            grantable_roles: [],
          });
        }),
        http.get("*/api/v1/validation/rule-sets/plat", notFound),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets/plat",
          () => HttpResponse.json(platform()),
        ),
      ],
    });
    expect(await screen.findByText("Warn until 29 Nov 2026")).toBeVisible();
    for (const name of [
      /^Pause/,
      /^Resume/,
      /Change date/,
      /Enforce version/,
    ]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("reads Paused in the Available here list", async () => {
    renderApp("/folders/acme-id/rule-sets", {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/validation/folders/acme-id/rule-sets", () =>
          HttpResponse.json({ items: [] }),
        ),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets",
          () =>
            HttpResponse.json({
              items: [
                platform({
                  paused: true,
                  paused_at: "2026-11-18T10:00:00Z",
                  resolved: [
                    {
                      version_no: 1,
                      mode: "warn",
                      enforce_at: "2026-11-29T00:00:00Z",
                      paused: true,
                    },
                  ],
                }),
              ],
            }),
        ),
      ],
    });
    const link = await screen.findByRole("link", { name: "odcs-consistency" });
    expect(
      within(link.closest("tr") as HTMLElement).getByText("Paused"),
    ).toBeVisible();
  });
});
