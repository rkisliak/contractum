// Rule-set screens beyond the main journey: the /rule-sets page and its two lists (empty, error,
// archived), creating a rule set, the Versions tab (view rules, deprecate), the page's
// error, not-found and Impact paths, and every draft-editor branch (add, remove, JSON edit,
// delete, the check outcomes).
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  AvailableRuleSet,
  RuleDef,
  RuleSet,
  RuleSetDraft,
  RuleSetVersionSummary,
} from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder } from "@/test/renderApp";
import { defaultFolder } from "./RuleSetsPage";

vi.setConfig({ testTimeout: 30000 });

afterEach(() => {
  tokenStore.clear();
});

const V = "*/api/v1/validation";

const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const folders = [rootFolder, acme];
const OWNER = [
  "ruleset:create",
  "ruleset:read",
  "ruleset:update",
  "ruleset:publish",
  "ruleset:deprecate",
  "ruleset:mandate",
  "ruleset:dry-run",
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

function rule(key: string, overrides: Partial<RuleDef> = {}): RuleDef {
  return {
    key,
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
    ...overrides,
  };
}

function draft(overrides: Partial<RuleSetDraft> = {}): RuleSetDraft {
  return {
    rule_set: ruleSet(),
    rules: [rule("zeta-rule", { severity: "warning" }), rule("alpha-rule")],
    check: null,
    ...overrides,
  };
}

function summary(
  n: number,
  overrides: Partial<RuleSetVersionSummary> = {},
): RuleSetVersionSummary {
  return {
    version_no: n,
    content_sha256: `sha-${String(n)}`,
    rule_keys: ["alpha-rule"],
    deprecated_at: null,
    enforce_at: null,
    published_at: "2026-11-10T10:00:00Z",
    published_by: "local:admin",
    ...overrides,
  };
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
    latest_version: 2,
    latest_active_version: 2,
    mandatory: false,
    paused: false,
    paused_at: null,
    versions: [summary(1), summary(2)],
    resolved: [],
    ...overrides,
  };
}

function problem(
  status: number,
  slug: string,
  extra: Record<string, unknown> = {},
) {
  return HttpResponse.json(
    {
      type: `urn:dc:problem:${slug}`,
      title: `Title of ${slug}`,
      status,
      detail: `${slug} detail`,
      ...extra,
    },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

function ownerHandlers(
  body: RuleSetDraft,
  extra: RequestHandler[] = [],
  versions: RuleSetVersionSummary[] = [],
): RequestHandler[] {
  return [
    ...extra,
    http.get(`${V}/rule-sets/rs-1`, () => HttpResponse.json(body)),
    http.get(`${V}/rule-sets/rs-1/versions`, () =>
      HttpResponse.json({ items: versions }),
    ),
    http.get(`${V}/folders/acme-id/available-rule-sets/rs-1`, () =>
      HttpResponse.json(available()),
    ),
  ];
}

/** The row-header texts of a table, in display order. */
function rowNames(table: HTMLElement): string[] {
  return within(table)
    .getAllByRole("rowheader")
    .map((cell) => cell.textContent);
}

// ---------------------------------------------------------------- /rule-sets and the lists

describe("rule sets page", () => {
  it("defaults to ROOT, lists both sections and switches folder through the URL", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/rule-sets", {
      folders,
      permissions: OWNER,
      handlers: [
        http.get(`${V}/folders/:id/rule-sets`, ({ params }) =>
          HttpResponse.json({
            items:
              params.id === "acme-id"
                ? [
                    ruleSet(),
                    ruleSet({
                      id: "rs-2",
                      name: "acme-strict",
                      mandatory: true,
                      publish_counter: 2,
                      draft_revision: 7,
                    }),
                  ]
                : [],
          }),
        ),
        http.get(`${V}/folders/:id/available-rule-sets`, ({ params }) =>
          HttpResponse.json({
            items:
              params.id === "acme-id"
                ? [
                    available({
                      id: "rs-9",
                      name: "acme-zeta",
                      mandatory: true,
                    }),
                    available({ latest_active_version: null }),
                  ]
                : [],
          }),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Rule sets" }),
    ).toBeVisible();
    const select = await screen.findByRole("combobox", { name: "Folder" });
    await waitFor(() => {
      expect(select).toHaveValue(rootFolder.id);
    });
    expect(await screen.findByText("No rule sets defined here")).toBeVisible();
    expect(
      screen.getByText(/Create the first rule set, for example/),
    ).toBeVisible();
    expect(screen.getByText("No rule sets apply here")).toBeVisible();

    await user.selectOptions(select, "acme-id");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ folder_id: "acme-id" });
    });
    const strict = await screen.findByRole("link", { name: "acme-strict" });
    const strictRow = strict.closest("tr") as HTMLElement;
    expect(within(strictRow).getByText("Mandatory")).toBeVisible();
    expect(within(strictRow).getByText("2")).toBeVisible();
    expect(within(strictRow).getByText("7")).toBeVisible();
    expect(within(strictRow).getByText("Organization")).toBeVisible();
    const baseRow = screen
      .getAllByRole("link", { name: "acme-baseline" })[0]
      ?.closest("tr") as HTMLElement;
    expect(within(baseRow).getByText("Optional")).toBeVisible();
    // The inherited copy has no active version left.
    const inherited = screen.getByRole("table", {
      name: "Rule sets available in Acme",
    });
    expect(within(inherited).getByText("Deprecated")).toBeVisible();
    expect(within(inherited).getByText("Not mandatory")).toBeVisible();

    // Client sorting on the sortable columns of both lists.
    const defined = screen.getByRole("table", {
      name: "Rule sets defined in Acme",
    });
    await user.click(
      within(defined).getByRole("button", { name: "Published versions" }),
    );
    expect(rowNames(defined)).toEqual(["acme-baseline", "acme-strict"]);
    await user.click(
      within(defined).getByRole("button", { name: "Published versions" }),
    );
    expect(rowNames(defined)).toEqual(["acme-strict", "acme-baseline"]);
    await user.click(within(defined).getByRole("button", { name: "Name" }));
    expect(
      within(defined).getByRole("columnheader", { name: "Name" }),
    ).toHaveAttribute("aria-sort", "ascending");
    expect(rowNames(defined)).toEqual(["acme-baseline", "acme-strict"]);
    await user.click(within(inherited).getByRole("button", { name: "Name" }));
    expect(
      within(inherited).getByRole("columnheader", { name: "Name" }),
    ).toHaveAttribute("aria-sort", "ascending");
    expect(rowNames(inherited)).toEqual(["acme-baseline", "acme-zeta"]);
  });

  it("shows a folder list failure with a retry", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/rule-sets", {
      folders,
      permissions: OWNER,
      handlers: [
        http.get("*/api/v1/identity/folders", () => {
          calls += 1;
          return problem(500, "folders-broken");
        }),
      ],
    });
    expect(await screen.findByText("folders-broken detail")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    await waitFor(() => {
      expect(calls).toBeGreaterThanOrEqual(2);
    });
  });

  it("says so when no folder is visible", async () => {
    renderApp("/rule-sets", { folders: [], permissions: OWNER });
    expect(
      await screen.findByText("You cannot see any folder yet."),
    ).toBeVisible();
  });

  it("picks the first folder by path when ROOT is not visible", () => {
    const b = makeFolder({ id: "b", path: "b" });
    const a = makeFolder({ id: "a", path: "a" });
    expect(defaultFolder([b, a])?.id).toBe("a");
    expect(defaultFolder([b, rootFolder, a])?.id).toBe(rootFolder.id);
    expect(defaultFolder([])).toBeUndefined();
  });

  it("offers no create in an archived folder and retries failed lists", async () => {
    const user = userEvent.setup();
    let defined = 0;
    let avail = 0;
    renderApp("/rule-sets?folder_id=acme-id", {
      folders: [rootFolder, { ...acme, archived: true }],
      permissions: OWNER,
      handlers: [
        http.get("*/api/v1/identity/folders", () =>
          HttpResponse.json({
            items: [rootFolder, { ...acme, archived: true }],
            next_cursor: null,
          }),
        ),
        http.get(`${V}/folders/acme-id/rule-sets`, () => {
          defined += 1;
          return defined === 1
            ? problem(500, "defined-broken")
            : HttpResponse.json({ items: [] });
        }),
        http.get(`${V}/folders/acme-id/available-rule-sets`, () => {
          avail += 1;
          return avail === 1
            ? problem(500, "available-broken")
            : HttpResponse.json({ items: [] });
        }),
      ],
    });
    expect(await screen.findByText("defined-broken detail")).toBeVisible();
    expect(await screen.findByText("available-broken detail")).toBeVisible();
    for (const button of screen.getAllByRole("button", {
      name: "Retry loading",
    }))
      await user.click(button);
    expect(
      await screen.findByText("Rule sets created at this folder appear here."),
    ).toBeVisible();
    expect(await screen.findByText("No rule sets apply here")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Create rule set" }),
    ).toBeNull();
  });
});

describe("create rule set dialog", () => {
  function handlers(onPost: (body: unknown) => Response): RequestHandler[] {
    return [
      http.get(`${V}/folders/acme-id/rule-sets`, () =>
        HttpResponse.json({ items: [] }),
      ),
      http.get(`${V}/folders/acme-id/available-rule-sets`, () =>
        HttpResponse.json({ items: [] }),
      ),
      http.post(`${V}/folders/acme-id/rule-sets`, async ({ request }) =>
        onPost(await request.json()),
      ),
    ];
  }

  it("requires a name, then creates and opens the new rule set", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    const { router } = renderApp("/rule-sets?folder_id=acme-id", {
      folders,
      permissions: OWNER,
      handlers: [
        ...handlers((body) => {
          bodies.push(body);
          return HttpResponse.json(ruleSet({ id: "rs-new" }), { status: 201 });
        }),
        ...ownerHandlers(draft()),
        http.get(`${V}/rule-sets/rs-new`, () =>
          HttpResponse.json(
            draft({ rule_set: ruleSet({ id: "rs-new" }), rules: [] }),
          ),
        ),
        http.get(`${V}/rule-sets/rs-new/versions`, () =>
          HttpResponse.json({ items: [] }),
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create rule set" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Create rule set",
    });
    expect(
      within(dialog).getByText(/The rule set belongs to Acme\./),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Create rule set" }),
    );
    expect(
      await within(dialog).findByText("Enter a name for the rule set."),
    ).toBeVisible();
    expect(bodies).toEqual([]);
    await user.type(
      within(dialog).getByRole("textbox", { name: "Name" }),
      "  acme-new  ",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Create rule set" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/rule-sets/rs-new");
    });
    expect(bodies).toEqual([{ name: "acme-new", description: null }]);
  });

  it("explains a duplicate name and maps field errors, then resets on Go back", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/rule-sets?folder_id=acme-id", {
      folders,
      permissions: OWNER,
      handlers: handlers((body) => {
        bodies.push(body);
        return bodies.length === 1
          ? problem(409, "rule-set-exists")
          : problem(422, "validation-error", {
              errors: [
                { pointer: "/name", detail: "Too odd." },
                { pointer: "/description", detail: "Too long." },
              ],
            });
      }),
    });
    await user.click(
      await screen.findByRole("button", { name: "Create rule set" }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.type(
      within(dialog).getByRole("textbox", { name: "Name" }),
      "dup",
    );
    await user.type(
      within(dialog).getByRole("textbox", { name: /Description/ }),
      "  Some text ",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Create rule set" }),
    );
    expect(
      await within(dialog).findByText(
        "A rule set with this name already exists here.",
      ),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Create rule set" }),
    );
    expect(await within(dialog).findAllByText("Too odd.")).not.toHaveLength(0);
    expect(within(dialog).getByText(/Too long\./)).toBeVisible();
    expect(bodies[1]).toEqual({ name: "dup", description: "Some text" });

    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await user.click(screen.getByRole("button", { name: "Create rule set" }));
    const again = await screen.findByRole("dialog");
    expect(within(again).getByRole("textbox", { name: "Name" })).toHaveValue(
      "",
    );
    expect(within(again).queryByText("Too odd.")).toBeNull();

    // Closing with Escape also starts the next opening fresh.
    await user.type(within(again).getByRole("textbox", { name: "Name" }), "x");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await user.click(screen.getByRole("button", { name: "Create rule set" }));
    expect(
      within(await screen.findByRole("dialog")).getByRole("textbox", {
        name: "Name",
      }),
    ).toHaveValue("");
  });
});

// ---------------------------------------------------------------- Versions tab

describe("rule set versions tab", () => {
  it("lists versions newest first, shows a version's rules read-only and deprecates one", async () => {
    const user = userEvent.setup();
    let deprecated = 0;
    renderApp("/rule-sets/rs-1?tab=versions", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(
        draft({ rule_set: ruleSet({ publish_counter: 2 }) }),
        [
          http.get(`${V}/rule-sets/rs-1/versions/2`, () =>
            HttpResponse.json({
              id: "v2",
              rule_set_id: "rs-1",
              version_no: 2,
              dsl_version: "cel-2",
              rules: [rule("alpha-rule")],
              rule_keys: ["alpha-rule"],
              content_sha256: "x",
              enforce_at: null,
              deprecated_at: null,
              deprecated_by: null,
              published_at: "2026-11-10T10:00:00Z",
              published_by: "local:admin",
            }),
          ),
          http.get(`${V}/rule-sets/rs-1/versions/1`, () =>
            problem(500, "version-broken"),
          ),
          http.post(`${V}/rule-sets/rs-1/versions/2/deprecate`, () => {
            deprecated += 1;
            return new HttpResponse(null, { status: 204 });
          }),
        ],
        [
          summary(1, { deprecated_at: "2026-11-11T00:00:00Z" }),
          summary(2, { enforce_at: "2026-11-29T00:00:00Z" }),
        ],
      ),
    });
    const table = await screen.findByRole("table", {
      name: "Versions of acme-baseline",
    });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(
      rows.map((row) => within(row).getAllByRole("rowheader")[0]?.textContent),
    ).toEqual(["Version 2", "Version 1"]);
    await user.click(within(table).getByRole("button", { name: "Version" }));
    expect(rowNames(table)).toEqual(["Version 1", "Version 2"]);
    await user.click(within(table).getByRole("button", { name: "Version" }));
    expect(rowNames(table)).toEqual(["Version 2", "Version 1"]);
    expect(
      within(rows[0] as HTMLElement).getByText("29 Nov 2026 (UTC)"),
    ).toBeVisible();
    expect(within(rows[0] as HTMLElement).getByText("Active")).toBeVisible();
    expect(
      within(rows[1] as HTMLElement).getByText("Not mandatory"),
    ).toBeVisible();
    expect(
      within(rows[1] as HTMLElement).getByText("Deprecated"),
    ).toBeVisible();
    expect(
      within(table).queryByRole("button", { name: "Deprecate version 1" }),
    ).toBeNull();

    await user.click(
      within(table).getByRole("button", { name: "View rules of version 2" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Version 2 of acme-baseline",
    });
    expect(await within(dialog).findByText("cel-2")).toBeVisible();
    const section = within(dialog).getByRole("region", {
      name: "Rule alpha-rule",
    });
    expect(within(section).getByLabelText("Condition (CEL)")).toHaveAttribute(
      "readonly",
    );
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    await user.click(
      within(table).getByRole("button", { name: "View rules of version 1" }),
    );
    expect(
      await within(await screen.findByRole("dialog")).findByText(
        "version-broken detail",
      ),
    ).toBeVisible();
    await user.keyboard("{Escape}");

    await user.click(
      within(table).getByRole("button", { name: "Deprecate version 2" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Deprecate version 2?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Deprecate version" }),
    );
    await waitFor(() => {
      expect(deprecated).toBe(1);
    });
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("shows the empty state, and the versions failure", async () => {
    renderApp("/rule-sets/rs-1?tab=versions", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(draft()),
    });
    expect(await screen.findByText("No published versions")).toBeVisible();
  });

  it("shows a versions list failure in the tab", async () => {
    renderApp("/rule-sets/rs-1?tab=versions", {
      folders,
      permissions: OWNER,
      handlers: [
        http.get(`${V}/rule-sets/rs-1/versions`, () =>
          problem(500, "versions-broken"),
        ),
        ...ownerHandlers(draft()),
      ],
    });
    expect(await screen.findByText("versions-broken detail")).toBeVisible();
  });

  it("reads a version's rules through the folder view when read-only", async () => {
    const user = userEvent.setup();
    let ownerRequested = false;
    renderApp("/rule-sets/rs-1?via=acme-id&tab=versions", {
      folders,
      permissions: ["ruleset:read-published"],
      handlers: [
        http.get(`${V}/rule-sets/rs-1`, () => {
          ownerRequested = true;
          return problem(404, "not-found");
        }),
        http.get(`${V}/folders/acme-id/available-rule-sets/rs-1`, () =>
          HttpResponse.json(available({ description: "Shared baseline" })),
        ),
        http.get(
          `${V}/folders/acme-id/available-rule-sets/rs-1/versions/2`,
          () =>
            HttpResponse.json({
              id: "v2",
              rule_set_id: "rs-1",
              version_no: 2,
              dsl_version: "cel-1",
              rules: [rule("alpha-rule")],
              rule_keys: ["alpha-rule"],
              content_sha256: "x",
              enforce_at: null,
              deprecated_at: null,
              deprecated_by: null,
              published_at: "2026-11-10T10:00:00Z",
              published_by: "local:admin",
            }),
        ),
      ],
    });
    expect(await screen.findByText("Read only")).toBeVisible();
    expect(screen.getByText(/Shared baseline/)).toBeVisible();
    expect(screen.queryByRole("tab", { name: "Impact" })).toBeNull();
    await user.click(
      await screen.findByRole("button", { name: "View rules of version 2" }),
    );
    expect(
      await within(await screen.findByRole("dialog")).findByText("cel-1"),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /^Deprecate version/ }),
    ).toBeNull();
    expect(ownerRequested).toBe(false);
  });
});

// ---------------------------------------------------------------- Page states and Impact

describe("rule set page states", () => {
  it("shows an owner read failure with a retry", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: [
        http.get(`${V}/rule-sets/rs-1`, () => {
          calls += 1;
          return problem(500, "owner-broken");
        }),
      ],
    });
    expect(await screen.findByText("owner-broken detail")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    await waitFor(() => {
      expect(calls).toBe(2);
    });
  });

  it("shows Not found when neither the owner nor the folder view has it", async () => {
    renderApp("/rule-sets/rs-x", {
      folders,
      permissions: OWNER,
      handlers: [
        http.get(`${V}/rule-sets/rs-x`, () => problem(404, "not-found")),
        http.get(`${V}/folders/:folder/available-rule-sets/rs-x`, () =>
          problem(404, "not-found"),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "Not found" }),
    ).toBeVisible();
  });

  it("shows Not found when the owner is gone and no folder view is visible", async () => {
    renderApp("/rule-sets/rs-x", {
      folders: [acme],
      permissions: OWNER,
      handlers: [
        http.get(`${V}/rule-sets/rs-x`, () => problem(404, "not-found")),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "Not found" }),
    ).toBeVisible();
  });

  it("offers the newest active version first, and the draft alone before any publish", async () => {
    renderApp("/rule-sets/rs-1?tab=impact", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(
        draft({ rule_set: ruleSet({ publish_counter: 3 }) }),
        [],
        [summary(1), summary(3), summary(2)],
      ),
    });
    const select = await screen.findByRole("combobox", { name: "Version" });
    await waitFor(() => {
      expect(
        within(select)
          .getAllByRole("option")
          .map((option) => option.textContent),
      ).toEqual([
        "Choose a version",
        "Version 3",
        "Version 2",
        "Version 1",
        "Draft (unpublished)",
      ]);
    });
    expect(select).toHaveValue("3");
    cleanup();

    renderApp("/rule-sets/rs-1?tab=impact", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(draft()),
    });
    expect(
      await screen.findByRole("combobox", { name: "Version" }),
    ).toHaveValue("draft");
  });

  it("offers nothing to dry-run when a read-only set has no active version", async () => {
    renderApp("/rule-sets/rs-1?via=acme-id&tab=impact", {
      folders,
      permissions: ["ruleset:read-published", "ruleset:dry-run"],
      handlers: [
        http.get(`${V}/folders/acme-id/available-rule-sets/rs-1`, () =>
          HttpResponse.json(
            available({
              latest_active_version: null,
              versions: [summary(1, { deprecated_at: "2026-11-11T00:00:00Z" })],
            }),
          ),
        ),
      ],
    });
    const select = await screen.findByRole("combobox", { name: "Version" });
    expect(select).toHaveValue("");
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Choose a version"]);
    expect(screen.getByRole("button", { name: "Run dry-run" })).toBeDisabled();
  });

  it("explains that platform sets cannot be dry-run", async () => {
    renderApp("/rule-sets/plat?tab=impact", {
      folders,
      permissions: OWNER,
      handlers: [
        http.get(`${V}/rule-sets/plat`, () => problem(404, "not-found")),
        http.get(`${V}/folders/:folder/available-rule-sets/plat`, () =>
          HttpResponse.json(
            available({
              id: "plat",
              name: "odcs-structural",
              scope: "platform",
              folder_id: null,
              folder_kind: null,
              mandatory: true,
              resolved: [
                {
                  version_no: 2,
                  mode: "enforce",
                  enforce_at: null,
                  paused: false,
                },
              ],
            }),
          ),
        ),
      ],
    });
    expect(
      await screen.findByText(/Platform rule sets apply everywhere/),
    ).toBeVisible();
    expect(screen.getByText("Owned by the platform")).toBeVisible();
    expect(screen.getByText("Enforced")).toBeVisible();
  });

  it("runs a dry-run of a version or the draft from the Impact tab", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    const { router } = renderApp("/rule-sets/rs-1?tab=impact", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(
        draft({ rule_set: ruleSet({ publish_counter: 2 }) }),
        [
          http.post(
            "*/api/v1/contracts/governance/rule-set-dry-runs",
            async ({ request }) => {
              bodies.push(await request.json());
              return HttpResponse.json({
                rule_set_id: "rs-1",
                version: 2,
                scope_folder_id: "acme-id",
                total_in_scope: 0,
                next_cursor: null,
                summary: { would_fail: 0, would_error: 0, pass: 0, hidden: 0 },
                items: [],
              });
            },
          ),
        ],
        [summary(1, { deprecated_at: "2026-11-11T00:00:00Z" }), summary(2)],
      ),
    });
    const select = await screen.findByRole("combobox", { name: "Version" });
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Choose a version", "Version 2", "Draft (unpublished)"]);
    expect(select).toHaveValue("2");
    expect(
      screen.getByRole("link", { name: "the Dry runs page" }),
    ).toHaveAttribute(
      "href",
      "/governance/dry-run?rule_set_id=rs-1&folder_id=acme-id",
    );
    await user.click(screen.getByRole("button", { name: "Run dry-run" }));
    await waitFor(() => {
      expect(bodies).toEqual([
        { rule_set_id: "rs-1", version: 2, folder_id: "acme-id" },
      ]);
    });
    await user.selectOptions(select, "draft");
    await user.click(screen.getByRole("button", { name: "Run dry-run" }));
    await waitFor(() => {
      expect(bodies[1]).toEqual({
        rule_set_id: "rs-1",
        version: "draft",
        folder_id: "acme-id",
      });
    });
    await user.selectOptions(select, "");
    expect(screen.getByRole("button", { name: "Run dry-run" })).toBeDisabled();

    await user.click(screen.getByRole("tab", { name: "Versions" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ tab: "versions" });
    });
  });

  it("hides the Impact tab without ruleset:dry-run and falls back to the first tab", async () => {
    renderApp("/rule-sets/rs-1?tab=impact", {
      folders,
      permissions: ["ruleset:read", "ruleset:update"],
      handlers: ownerHandlers(draft()),
    });
    expect(
      await screen.findByRole("tab", { name: "Draft", selected: true }),
    ).toBeVisible();
    expect(screen.queryByRole("tab", { name: "Impact" })).toBeNull();
  });
});

// ---------------------------------------------------------------- Draft editor branches

describe("rule draft editor", () => {
  it("lists rules sorted, switches between them and adds a new rule", async () => {
    const user = userEvent.setup();
    const puts: { path: string; ifMatch: string | null; body: unknown }[] = [];
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(draft(), [
        http.put(
          `${V}/rule-sets/rs-1/draft/rules/:key`,
          async ({ request, params }) => {
            const body = (await request.json()) as RuleDef;
            puts.push({
              path: String(params.key),
              ifMatch: request.headers.get("If-Match"),
              body,
            });
            return HttpResponse.json(
              draft({
                rule_set: ruleSet({ draft_revision: 4 }),
                rules: [...draft().rules, body],
                check: { status: "unavailable" },
              }),
            );
          },
        ),
      ]),
    });
    const nav = await screen.findByRole("navigation", {
      name: "Rules in the draft",
    });
    const buttons = within(nav)
      .getAllByRole("button")
      .filter((button) => button.textContent !== "Add rule");
    expect(buttons.map((button) => button.textContent)).toEqual([
      expect.stringContaining("alpha-rule"),
      expect.stringContaining("zeta-rule"),
    ]);
    expect(buttons[0]).toHaveAttribute("aria-current", "true");
    expect(within(nav).getAllByText("Not checked")).toHaveLength(2);
    await user.click(buttons[1] as HTMLElement);
    expect(
      await screen.findByRole("region", { name: "Rule zeta-rule" }),
    ).toBeVisible();
    expect(screen.getByLabelText("Severity")).toHaveValue("warning");

    await user.click(within(nav).getByRole("button", { name: "Add rule" }));
    const form = await screen.findByRole("region", { name: "New rule" });
    expect(within(form).getByLabelText("Key")).not.toHaveAttribute("readonly");
    expect(
      within(form).queryByRole("button", { name: "Remove rule" }),
    ).toBeNull();
    // Local shape checks first: nothing is sent.
    await user.click(
      within(form).getByRole("button", { name: "Add rule to draft" }),
    );
    expect(
      await within(form).findByText("Enter a CEL condition."),
    ).toBeVisible();
    expect(
      within(form).getByText(/Start with a lowercase letter/),
    ).toBeVisible();
    expect(puts).toEqual([]);

    await user.type(within(form).getByLabelText("Key"), "alpha-rule");
    await user.type(within(form).getByLabelText("Condition (CEL)"), "true");
    await user.type(within(form).getByLabelText("Message"), "Always.");
    await user.click(
      within(form).getByRole("button", { name: "Add rule to draft" }),
    );
    expect(
      await within(form).findByText(
        "A rule with this key already exists in the draft.",
      ),
    ).toBeVisible();
    expect(puts).toEqual([]);

    await user.clear(within(form).getByLabelText("Key"));
    await user.type(within(form).getByLabelText("Key"), "new-rule");
    await user.click(
      within(form).getByRole("button", { name: "Add rule to draft" }),
    );
    expect(await screen.findByText("Rule new-rule is saved.")).toBeVisible();
    expect(puts).toHaveLength(1);
    expect(puts[0]?.path).toBe("new-rule");
    expect(puts[0]?.ifMatch).toBe('"r3"');
    expect(puts[0]?.body).toMatchObject({
      key: "new-rule",
      condition: "true",
      message: "Always.",
      target: "$",
      fixtures: { pass: [{ document: {} }], fail: [{ document: {} }] },
    });
    expect(
      screen.getByText(
        "The rule check is unavailable right now. Run Check draft again in a moment.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Dismiss alert" }));
    expect(screen.queryByText("Rule new-rule is saved.")).toBeNull();
  });

  it("shows a save failure other than a conflict as a problem", async () => {
    const user = userEvent.setup();
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(draft(), [
        http.put(`${V}/rule-sets/rs-1/draft/rules/:key`, () =>
          problem(422, "rule-invalid"),
        ),
      ]),
    });
    await user.click(await screen.findByRole("button", { name: "Save rule" }));
    expect(await screen.findByText("rule-invalid detail")).toBeVisible();
  });

  it("reloads the draft after a conflict", async () => {
    const user = userEvent.setup();
    let gets = 0;
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: [
        http.get(`${V}/rule-sets/rs-1`, () => {
          gets += 1;
          return HttpResponse.json(
            gets === 1
              ? draft()
              : draft({
                  rule_set: ruleSet({ draft_revision: 9 }),
                  rules: [
                    rule("alpha-rule", { message: "Server wording." }),
                    rule("fresh-rule"),
                  ],
                }),
          );
        }),
        ...ownerHandlers(draft(), [
          http.put(`${V}/rule-sets/rs-1/draft/rules/:key`, () =>
            problem(412, "precondition-failed"),
          ),
        ]),
      ],
    });
    await user.click(await screen.findByRole("button", { name: "Save rule" }));
    // The server's current rule beside the refused one.
    const server = await screen.findByLabelText(
      "Server rule alpha-rule, revision 9",
    );
    expect(server).toHaveTextContent("Server wording.");
    expect(screen.getByLabelText("Your refused change")).toHaveTextContent(
      "Add a description.",
    );
    await user.click(
      await screen.findByRole("button", { name: "Reload draft" }),
    );
    // The editor restarts on the reloaded draft (first rule selected, server wording).
    expect(await screen.findByDisplayValue("Server wording.")).toBeVisible();
    expect(screen.getByText("fresh-rule")).toBeVisible();
    expect(screen.queryByText("Someone else changed this draft")).toBeNull();
  });

  it("removes a rule with If-Match and selects the next one", async () => {
    const user = userEvent.setup();
    let ifMatch: string | null = null;
    let removed = "";
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(draft(), [
        http.delete(
          `${V}/rule-sets/rs-1/draft/rules/:key`,
          ({ request, params }) => {
            ifMatch = request.headers.get("If-Match");
            removed = String(params.key);
            return HttpResponse.json(
              draft({
                rule_set: ruleSet({ draft_revision: 4 }),
                rules: [
                  rule("zeta-rule", { severity: "warning" }),
                  rule("omega-rule"),
                ],
              }),
            );
          },
        ),
      ]),
    });
    await user.click(
      await screen.findByRole("button", { name: "Remove rule" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Remove rule alpha-rule?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Remove rule" }),
    );
    expect(
      await screen.findByRole("region", { name: "Rule omega-rule" }),
    ).toBeVisible();
    expect(removed).toBe("alpha-rule");
    expect(ifMatch).toBe('"r3"');
  });

  it("selects nothing after removing the last rule", async () => {
    const user = userEvent.setup();
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(draft({ rules: [rule("alpha-rule")] }), [
        http.delete(`${V}/rule-sets/rs-1/draft/rules/alpha-rule`, () =>
          HttpResponse.json(draft({ rules: [] })),
        ),
      ]),
    });
    await user.click(
      await screen.findByRole("button", { name: "Remove rule" }),
    );
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Remove rule",
      }),
    );
    expect(await screen.findByText("No rule selected")).toBeVisible();
  });

  it("explains each check outcome and keeps Publish off until it is clean", async () => {
    const user = userEvent.setup();
    let call = 0;
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(
        draft({ rule_set: ruleSet({ mandatory: true }) }),
        [
          http.post(`${V}/rule-sets/rs-1/draft/check`, () => {
            call += 1;
            if (call === 1)
              return HttpResponse.json(
                draft({
                  check: {
                    ok: false,
                    rule_set_problems: [{ code: "duplicate_rule_key" }],
                    rules: [],
                  },
                }),
              );
            if (call === 2)
              return HttpResponse.json(
                draft({
                  check: {
                    ok: false,
                    rule_set_problems: [],
                    rules: [
                      {
                        rule_key: "alpha-rule",
                        ok: false,
                        problems: [{ code: "target_invalid", detail: "bad $" }],
                      },
                      { rule_key: "zeta-rule", ok: true, problems: [] },
                    ],
                  },
                }),
              );
            if (call === 3) return problem(503, "check-down");
            return HttpResponse.json(
              draft({
                check: {
                  ok: true,
                  rule_set_problems: [],
                  rules: [
                    { rule_key: "alpha-rule", ok: true, problems: [] },
                    { rule_key: "zeta-rule", ok: true, problems: [] },
                  ],
                },
              }),
            );
          }),
        ],
      ),
    });
    const publish = await screen.findByRole("button", {
      name: "Publish version 1",
    });
    expect(publish).toBeDisabled();
    expect(
      screen.getByText(
        "Run Check draft; publishing needs a clean check of this revision.",
      ),
    ).toBeVisible();
    const check = screen.getByRole("button", { name: "Check draft" });

    await user.click(check);
    expect(
      await screen.findByText("The draft does not pass its check"),
    ).toBeVisible();
    expect(screen.getByText("Two rules share a key")).toBeVisible();

    await user.click(check);
    expect(
      await screen.findByText(
        "Open the rules marked with problems to see what to fix.",
      ),
    ).toBeVisible();
    expect(screen.getByText("Has problems")).toBeVisible();
    expect(screen.getByText("Check passed")).toBeVisible();
    expect(
      screen.getByText("The target is not a valid JSONPath"),
    ).toBeVisible();

    await user.click(check);
    expect(
      await screen.findByText("Service temporarily unavailable"),
    ).toBeVisible();

    await user.click(check);
    expect(
      await screen.findByText(
        "The draft passes its check. You can publish it.",
      ),
    ).toBeVisible();
    expect(publish).toBeEnabled();
    await user.click(publish);
    const confirm = await screen.findByRole("alertdialog", {
      name: "Publish version 1 of acme-baseline?",
    });
    expect(
      within(confirm).getByText(
        /It is mandatory: it warns for the grace period/,
      ),
    ).toBeVisible();
    await user.click(within(confirm).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("replaces the draft as JSON after refusing invalid input", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    let mode: "problem" | "plain" | "network" | "ok" | "conflict" = "problem";
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(draft(), [
        http.put(`${V}/rule-sets/rs-1/draft`, async ({ request }) => {
          bodies.push(await request.json());
          if (mode === "problem")
            return problem(422, "validation-error", {
              errors: [{ pointer: "/rules/0/key", detail: "Bad key." }],
            });
          if (mode === "plain") return problem(400, "draft-refused");
          if (mode === "network") return HttpResponse.error();
          if (mode === "conflict") return problem(412, "precondition-failed");
          return HttpResponse.json(
            draft({
              rule_set: ruleSet({ draft_revision: 4 }),
              rules: [rule("imported-b"), rule("imported-a")],
            }),
          );
        }),
      ]),
    });
    await user.click(
      await screen.findByRole("button", { name: "Edit draft as JSON" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Edit draft as JSON",
    });
    const field = within(dialog).getByRole("textbox", { name: "Draft (JSON)" });
    expect(JSON.parse((field as HTMLTextAreaElement).value)).toEqual({
      description: "Baseline rules",
      rules: draft().rules,
    });
    const replace = within(dialog).getByRole("button", {
      name: "Replace draft",
    });

    await user.clear(field);
    await user.type(field, "not json");
    await user.click(replace);
    expect(
      await within(dialog).findByText("This is not valid JSON."),
    ).toBeVisible();

    await user.clear(field);
    await user.type(field, "[[]");
    await user.click(replace);
    expect(
      await within(dialog).findByText(
        'The draft is an object with a "rules" array.',
      ),
    ).toBeVisible();
    expect(bodies).toEqual([]);

    await user.clear(field);
    await user.type(field, '{{"rules": [[]}');
    await user.click(replace);
    expect(
      await within(dialog).findByText("/rules/0/key: Bad key."),
    ).toBeVisible();

    mode = "plain";
    await user.click(replace);
    expect(
      await within(dialog).findByText("draft-refused detail"),
    ).toBeVisible();

    mode = "network";
    await user.click(replace);
    expect(
      await within(dialog).findByText(
        "The server could not be reached. Try again.",
      ),
    ).toBeVisible();

    mode = "ok";
    await user.click(replace);
    expect(await screen.findByText("The draft is replaced.")).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(
      screen.getByRole("region", { name: "Rule imported-a" }),
    ).toBeVisible();
    expect(bodies.at(-1)).toEqual({ rules: [] });

    mode = "conflict";
    await user.click(
      screen.getByRole("button", { name: "Edit draft as JSON" }),
    );
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Replace draft",
      }),
    );
    expect(
      await screen.findByText("Someone else changed this draft"),
    ).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
    // A replaced draft is compared whole.
    expect(
      await screen.findByLabelText(/^Server draft, revision \d+$/),
    ).toBeVisible();
    expect(screen.getByLabelText("Your refused change")).toHaveTextContent(
      '"rules"',
    );
  });

  it("selects nothing when the JSON draft has no rules, and Go back closes it", async () => {
    const user = userEvent.setup();
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(draft(), [
        http.put(`${V}/rule-sets/rs-1/draft`, () =>
          HttpResponse.json(draft({ rules: [] })),
        ),
      ]),
    });
    await user.click(
      await screen.findByRole("button", { name: "Edit draft as JSON" }),
    );
    expect(await screen.findByRole("dialog")).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await user.click(
      screen.getByRole("button", { name: "Edit draft as JSON" }),
    );
    let dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await user.click(
      screen.getByRole("button", { name: "Edit draft as JSON" }),
    );
    dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Replace draft" }),
    );
    expect(await screen.findByText("No rule selected")).toBeVisible();
  });

  it("deletes a never-published rule set after typing its name", async () => {
    const user = userEvent.setup();
    let deleted = false;
    const { router } = renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(draft(), [
        http.delete(`${V}/rule-sets/rs-1`, () => {
          deleted = true;
          return new HttpResponse(null, { status: 204 });
        }),
        http.get(`${V}/folders/acme-id/rule-sets`, () =>
          HttpResponse.json({ items: [] }),
        ),
        http.get(`${V}/folders/acme-id/available-rule-sets`, () =>
          HttpResponse.json({ items: [] }),
        ),
      ]),
    });
    await user.click(
      await screen.findByRole("button", { name: "Delete rule set" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Delete acme-baseline?",
    });
    const go = within(confirm).getByRole("button", { name: "Delete rule set" });
    expect(go).toBeDisabled();
    await user.type(
      within(confirm).getByRole("textbox", {
        name: "Type acme-baseline to confirm",
      }),
      "acme-baseline",
    );
    await user.click(go);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/rule-sets");
    });
    expect(router.state.location.search).toEqual({ folder_id: "acme-id" });
    expect(deleted).toBe(true);
  });

  it("is read-only without update or publish rights, and explains an empty draft", async () => {
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: ["ruleset:read"],
      handlers: ownerHandlers(draft({ rules: [] })),
    });
    expect(
      await screen.findByText("The draft has no rules yet."),
    ).toBeVisible();
    expect(screen.getByText("This draft has no rules.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Check draft" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Add rule" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Edit draft as JSON" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /^Publish/ })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Delete rule set" }),
    ).toBeNull();
  });

  it("asks an editor to add the first rule", async () => {
    renderApp("/rule-sets/rs-1", {
      folders,
      permissions: OWNER,
      handlers: ownerHandlers(draft({ rules: [] })),
    });
    expect(
      await screen.findByText(/Add a rule: a JSONPath target, a CEL condition/),
    ).toBeVisible();
  });
});

describe("the name rule on the create rule set dialog", () => {
  it("shows the message for an emoji and sends nothing", async () => {
    const user = userEvent.setup();
    let posted = false;
    renderApp("/rule-sets?folder_id=acme-id", {
      folders,
      permissions: OWNER,
      handlers: [
        http.get(`${V}/folders/acme-id/rule-sets`, () =>
          HttpResponse.json({ items: [] }),
        ),
        http.get(`${V}/folders/acme-id/available-rule-sets`, () =>
          HttpResponse.json({ items: [] }),
        ),
        http.post(`${V}/folders/acme-id/rule-sets`, () => {
          posted = true;
          return HttpResponse.json({}, { status: 201 });
        }),
        ...ownerHandlers(draft()),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create rule set" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Create rule set",
    });
    await user.type(
      within(dialog).getByRole("textbox", { name: "Name" }),
      "acme 😀",
    );
    expect(
      within(dialog).getByText(
        "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.",
      ),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Create rule set" }),
    );
    expect(posted).toBe(false);
  });
});
