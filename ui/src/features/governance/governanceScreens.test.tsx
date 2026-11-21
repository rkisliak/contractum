// Dry-run form and drift report branches beyond the main journeys: building a dry-run from the
// form (folder, rule set, version or draft), the drift report's states (loading capabilities,
// error with retry, empty, paging with the cursor) and its client-side filters.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ImpactFailingRule } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder } from "@/test/renderApp";

vi.setConfig({ testTimeout: 30000 });

afterEach(() => {
  tokenStore.clear();
});

const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const payments = makeFolder({
  id: "payments-id",
  parent_id: "acme-id",
  kind: "project",
  slug: "payments",
  display_name: "Payments",
  path: "acme/payments",
});
const folders = [rootFolder, acme, payments];

function problem(status: number, slug: string) {
  return HttpResponse.json(
    {
      type: `urn:dc:problem:${slug}`,
      title: `Title of ${slug}`,
      status,
      detail: `${slug} detail`,
    },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

function failing(key: string, blocking = true): ImpactFailingRule {
  return {
    rule_key: key,
    rule_set_id: "rs-1",
    version_no: 1,
    severity: "error",
    mode: "enforce",
    outcome: "fail",
    blocking,
    error_code: null,
    message: "Broken.",
    pointers: [],
    pointers_total: 0,
    pointers_withheld: false,
  };
}

function versionSummary(n: number, deprecated = false) {
  return {
    version_no: n,
    content_sha256: "x",
    rule_keys: ["k"],
    deprecated_at: deprecated ? "2026-11-01T00:00:00Z" : null,
    enforce_at: null,
    published_at: "2026-11-01T00:00:00Z",
    published_by: "local:admin",
  };
}

describe("dry-run form", () => {
  it("builds a run from a folder, a rule set and a version or the draft", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    const { router } = renderApp("/governance/dry-run", {
      folders,
      handlers: [
        http.get(
          "*/api/v1/validation/folders/payments-id/available-rule-sets",
          () =>
            HttpResponse.json({
              items: [
                {
                  id: "inherited",
                  scope: "folder",
                  folder_id: "acme-id",
                  folder_kind: "organization",
                  name: "acme-baseline",
                  description: null,
                  latest_version: 3,
                  latest_active_version: 3,
                  mandatory: false,
                  versions: [
                    versionSummary(1),
                    versionSummary(2, true),
                    versionSummary(3),
                  ],
                  resolved: [],
                },
                {
                  id: "plat",
                  scope: "platform",
                  folder_id: null,
                  folder_kind: null,
                  name: "odcs-structural",
                  description: null,
                  latest_version: 1,
                  latest_active_version: 1,
                  mandatory: true,
                  versions: [versionSummary(1)],
                  resolved: [],
                },
                {
                  id: "own-published",
                  scope: "folder",
                  folder_id: "payments-id",
                  folder_kind: "project",
                  name: "payments-published",
                  description: null,
                  latest_version: 1,
                  latest_active_version: 1,
                  mandatory: false,
                  versions: [versionSummary(1)],
                  resolved: [],
                },
              ],
            }),
        ),
        http.get("*/api/v1/validation/folders/payments-id/rule-sets", () =>
          HttpResponse.json({
            items: [
              {
                id: "own-draft",
                scope: "folder",
                folder_id: "payments-id",
                folder_kind: "project",
                name: "payments-draft",
                description: null,
                publish_counter: 0,
                draft_revision: 1,
                mandatory: false,
                mandatory_set_at: null,
                mandatory_set_by: null,
                created_by: "local:admin",
                created_at: "2026-11-01T10:00:00Z",
                updated_by: "local:admin",
                updated_at: "2026-11-01T10:00:00Z",
              },
              {
                id: "own-published",
                scope: "folder",
                folder_id: "payments-id",
                folder_kind: "project",
                name: "payments-published",
                description: null,
                publish_counter: 1,
                draft_revision: 2,
                mandatory: false,
                mandatory_set_at: null,
                mandatory_set_by: null,
                created_by: "local:admin",
                created_at: "2026-11-01T10:00:00Z",
                updated_by: "local:admin",
                updated_at: "2026-11-01T10:00:00Z",
              },
            ],
          }),
        ),
        http.get(
          "*/api/v1/validation/folders/acme-id/available-rule-sets",
          () => HttpResponse.json({ items: [] }),
        ),
        http.get("*/api/v1/validation/folders/acme-id/rule-sets", () =>
          HttpResponse.json({ items: [] }),
        ),
        http.post(
          "*/api/v1/contracts/governance/rule-set-dry-runs",
          async ({ request }) => {
            bodies.push(await request.json());
            return HttpResponse.json({
              rule_set_id: "x",
              version: 1,
              scope_folder_id: "payments-id",
              total_in_scope: 1,
              next_cursor: null,
              summary: { would_fail: 0, would_error: 0, pass: 1, hidden: 0 },
              items: [],
            });
          },
        ),
      ],
    });
    expect(
      await screen.findByText(
        "Choose a scope folder, a rule set and a version, then run the dry-run.",
      ),
    ).toBeVisible();
    const run = screen.getByRole("button", { name: "Run dry-run" });
    expect(run).toBeDisabled();
    const ruleSetSelect = screen.getByRole("combobox", { name: "Rule set" });
    expect(ruleSetSelect).toBeDisabled();
    expect(within(ruleSetSelect).getByRole("option")).toHaveTextContent(
      "Choose a folder first",
    );
    const folderSelect = screen.getByRole("combobox", { name: "Scope folder" });
    await waitFor(() => {
      expect(folderSelect).toBeEnabled();
    });
    // ROOT cannot be a dry-run scope.
    expect(
      within(folderSelect)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Choose a folder", "Acme (acme)", "Payments (acme/payments)"]);

    await user.selectOptions(folderSelect, "payments-id");
    await waitFor(() => {
      expect(within(ruleSetSelect).getAllByRole("option")).toHaveLength(4);
    });
    // Platform sets are left out; own sets add the draft.
    expect(
      within(ruleSetSelect)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([
      "Choose a rule set",
      "acme-baseline",
      "payments-published",
      "payments-draft",
    ]);
    const versionSelect = screen.getByRole("combobox", { name: "Version" });
    expect(versionSelect).toBeDisabled();

    await user.selectOptions(ruleSetSelect, "inherited");
    expect(versionSelect).toHaveValue("3");
    expect(
      within(versionSelect)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Choose a version", "Version 3", "Version 1"]);
    await user.selectOptions(versionSelect, "1");
    await user.click(run);
    await waitFor(() => {
      expect(bodies).toEqual([
        { rule_set_id: "inherited", version: 1, folder_id: "payments-id" },
      ]);
    });
    expect(router.state.location.search).toEqual({
      rule_set_id: "inherited",
      version: 1,
      folder_id: "payments-id",
    });

    await user.selectOptions(ruleSetSelect, "own-draft");
    expect(versionSelect).toHaveValue("draft");
    await user.click(run);
    await waitFor(() => {
      expect(bodies[1]).toEqual({
        rule_set_id: "own-draft",
        version: "draft",
        folder_id: "payments-id",
      });
    });

    await user.selectOptions(ruleSetSelect, "own-published");
    expect(
      within(versionSelect)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Choose a version", "Version 1", "Draft (unpublished)"]);
    expect(versionSelect).toHaveValue("1");

    await user.selectOptions(ruleSetSelect, "");
    expect(versionSelect).toHaveValue("");
    expect(run).toBeDisabled();

    // A new folder clears the rule set and the version.
    await user.selectOptions(folderSelect, "acme-id");
    expect(ruleSetSelect).toHaveValue("");
  });
});

function driftItem(id: string, overrides: Record<string, unknown> = {}) {
  return {
    contract_id: id,
    folder_id: "acme-id",
    name: `Contract ${id}`,
    odcs_id: `urn:${id}`,
    latest_semver: "1.0.0",
    status: "drifted",
    counts: { fail: 1, error: 0, warn: 2 },
    failing_rules: [failing("rule-a")],
    pinned_rule_sets: [],
    current_versions: [],
    ...overrides,
  };
}

function driftPage(
  items: ReturnType<typeof driftItem>[],
  next: string | null,
  totals: Partial<Record<string, number>> = {},
) {
  return {
    folder_id: "acme-id",
    total_in_scope: 1,
    next_cursor: next,
    summary: {
      drifted: 0,
      at_risk: 0,
      compliant: 0,
      unparseable: 0,
      hidden: 0,
      ...totals,
    },
    items,
  };
}

describe("drift report", () => {
  it("pages with the cursor, adds up the summary and filters the loaded rows", async () => {
    const user = userEvent.setup();
    const cursors: (string | null)[] = [];
    renderApp("/governance/drift?folder_id=acme-id", {
      folders,
      permissions: ["governance:drift-read"],
      handlers: [
        http.get("*/api/v1/contracts/governance/drift", ({ request }) => {
          const cursor = new URL(request.url).searchParams.get("cursor");
          cursors.push(cursor);
          return HttpResponse.json(
            cursor === null
              ? driftPage(
                  [
                    driftItem("c-1", {
                      pinned_rule_sets: [
                        { rule_set_id: "rs-1", name: "acme", version: 2 },
                        { rule_set_id: "rs-2", name: null, version: null },
                      ],
                      current_versions: [
                        {
                          rule_set_id: "rs-1",
                          name: "acme",
                          version_no: 3,
                          mode: "enforce",
                        },
                      ],
                    }),
                    driftItem("c-2", {
                      name: null,
                      status: "at_risk",
                      folder_id: "payments-id",
                      latest_semver: null,
                      failing_rules: [failing("rule-b", false)],
                    }),
                  ],
                  "page-2",
                  { drifted: 1, at_risk: 1, compliant: 4, hidden: 1 },
                )
              : driftPage(
                  [
                    driftItem("c-3", {
                      status: "unparseable",
                      failing_rules: [],
                    }),
                  ],
                  null,
                  { unparseable: 1, compliant: 2 },
                ),
          );
        }),
      ],
    });
    expect(await screen.findByText("1 contract in scope")).toBeVisible();
    const stats = screen.getByLabelText("Drift summary");
    const value = (label: string) =>
      within(stats).getByText(label).nextElementSibling?.textContent;
    expect(value("Drifted")).toBe("1");
    expect(value("Compliant")).toBe("4");

    const first = screen.getByRole("link", { name: "Contract c-1" });
    const firstRow = first.closest("tr") as HTMLElement;
    expect(
      within(firstRow).getByText("Pinned: acme v2, rule set v?"),
    ).toBeVisible();
    expect(
      within(firstRow).getByText("Current: acme v3 (enforce)"),
    ).toBeVisible();
    expect(within(firstRow).getByText(/^1\s\/\s0\s\/\s2$/)).toBeVisible();
    const second = screen.getByRole("link", { name: "urn:c-2" });
    const secondRow = second.closest("tr") as HTMLElement;
    expect(within(secondRow).getByText("At risk")).toBeVisible();
    expect(within(secondRow).getByText("Not published")).toBeVisible();
    expect(within(secondRow).getByText("acme/payments")).toBeVisible();
    expect(within(secondRow).getByText("Pinned: none")).toBeVisible();
    expect(within(secondRow).getByText("Current: none")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Load more results" }));
    const third = await screen.findByRole("link", { name: "Contract c-3" });
    expect(
      within(third.closest("tr") as HTMLElement).getByText("Unparseable"),
    ).toBeVisible();
    expect(
      within(third.closest("tr") as HTMLElement).getByText("None"),
    ).toBeVisible();
    expect(cursors).toEqual([null, "page-2"]);
    expect(value("Compliant")).toBe("6");
    expect(value("Unparseable")).toBe("1");
    expect(screen.getByText("All results loaded")).toBeVisible();

    const table = screen.getByRole("table", { name: "Drifted contracts" });
    await user.click(
      within(table).getByRole("button", { name: "Contract and status" }),
    );
    // Sorted by name, or the ODCS id when there is no name.
    expect(
      within(table)
        .getAllByRole("rowheader")
        .map((cell) => within(cell).getByRole("link").textContent),
    ).toEqual(["Contract c-1", "Contract c-3", "urn:c-2"]);

    const filters = screen.getByRole("group", {
      name: "Filter loaded results",
    });
    await user.selectOptions(
      within(filters).getByRole("combobox", { name: "Status" }),
      "at_risk",
    );
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "Contract c-1" })).toBeNull();
    });
    expect(screen.getByRole("link", { name: "urn:c-2" })).toBeVisible();
    await user.selectOptions(
      within(filters).getByRole("combobox", { name: "Status" }),
      "",
    );
    await user.selectOptions(
      within(filters).getByRole("combobox", { name: "Failing rule" }),
      "rule-a",
    );
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "urn:c-2" })).toBeNull();
    });
    expect(screen.getByRole("link", { name: "Contract c-1" })).toBeVisible();
    await user.selectOptions(
      within(filters).getByRole("combobox", { name: "Failing rule" }),
      "",
    );
    // The folder filter has no accessible name (see the skipped test below), so it is found by
    // its option labels instead.
    const folderFilter = within(filters)
      .getAllByRole("combobox")
      .find((select) =>
        within(select).queryByRole("option", { name: "Any folder" }),
      ) as HTMLElement;
    expect(
      within(folderFilter)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Any folder", "acme", "acme/payments"]);
    await user.selectOptions(folderFilter, "payments-id");
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "Contract c-3" })).toBeNull();
    });
    expect(screen.getByRole("link", { name: "urn:c-2" })).toBeVisible();
  });

  // Regression: the page picker and the filter bar's folder select shared id="drift-folder", so
  // the filter's select had no accessible name.
  it("names the drift folder filter for assistive technology", async () => {
    renderApp("/governance/drift?folder_id=acme-id", {
      folders,
      permissions: ["governance:drift-read"],
      handlers: [
        http.get("*/api/v1/contracts/governance/drift", () =>
          HttpResponse.json(driftPage([driftItem("c-1")], null)),
        ),
      ],
    });
    const filters = await screen.findByRole("group", {
      name: "Filter loaded results",
    });
    expect(
      within(filters).getByRole("combobox", { name: "Folder" }),
    ).toBeVisible();
  });

  it("keeps the loaded rows when the next page fails", async () => {
    const user = userEvent.setup();
    renderApp("/governance/drift?folder_id=acme-id", {
      folders,
      permissions: ["governance:drift-read"],
      handlers: [
        http.get("*/api/v1/contracts/governance/drift", ({ request }) =>
          new URL(request.url).searchParams.get("cursor") === null
            ? HttpResponse.json(driftPage([driftItem("c-1")], "next"))
            : HttpResponse.json(
                {
                  type: "urn:dc:problem:broken",
                  title: "Drift page broke",
                  status: 500,
                },
                {
                  status: 500,
                  headers: { "Content-Type": "application/problem+json" },
                },
              ),
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Load more results" }),
    );
    expect(await screen.findByText("Drift page broke")).toBeVisible();
    expect(screen.getByRole("link", { name: "Contract c-1" })).toBeVisible();
  });

  it("treats a drift answer without a body as a failure", async () => {
    renderApp("/governance/drift?folder_id=acme-id", {
      folders,
      permissions: ["governance:drift-read"],
      handlers: [
        http.get(
          "*/api/v1/contracts/governance/drift",
          () => new HttpResponse(null, { status: 204 }),
        ),
      ],
    });
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });

  it("shows the empty state when nothing drifted", async () => {
    renderApp("/governance/drift?folder_id=acme-id", {
      folders,
      permissions: ["governance:drift-read"],
      handlers: [
        http.get("*/api/v1/contracts/governance/drift", () => {
          const page = driftPage([], null, { compliant: 3 });
          return HttpResponse.json({ ...page, total_in_scope: 3 });
        }),
      ],
    });
    expect(await screen.findByText("No drift")).toBeVisible();
    expect(screen.getByText("3 contracts in scope")).toBeVisible();
    expect(
      screen.queryByRole("group", { name: "Filter loaded results" }),
    ).toBeNull();
  });

  it("shows a failed report with a retry", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/governance/drift?folder_id=acme-id", {
      folders,
      permissions: ["governance:drift-read"],
      handlers: [
        http.get("*/api/v1/contracts/governance/drift", () => {
          calls += 1;
          return calls === 1
            ? problem(500, "drift-broken")
            : HttpResponse.json(driftPage([], null));
        }),
      ],
    });
    expect(await screen.findByText("drift-broken detail")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(await screen.findByText("No drift")).toBeVisible();
  });

  it("defaults to ROOT and switches folder through the URL", async () => {
    const user = userEvent.setup();
    const asked: (string | null)[] = [];
    const { router } = renderApp("/governance/drift", {
      folders,
      permissions: ["governance:drift-read"],
      handlers: [
        http.get("*/api/v1/contracts/governance/drift", ({ request }) => {
          asked.push(new URL(request.url).searchParams.get("folder_id"));
          return HttpResponse.json(driftPage([], null));
        }),
      ],
    });
    const select = await screen.findByRole("combobox", { name: "Folder" });
    await waitFor(() => {
      expect(asked).toEqual([rootFolder.id]);
    });
    expect(select).toHaveValue(rootFolder.id);
    await user.selectOptions(select, "acme-id");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ folder_id: "acme-id" });
    });
    await waitFor(() => {
      expect(asked).toEqual([rootFolder.id, "acme-id"]);
    });
  });

  it("falls back to the first folder without ROOT, and says so when none is visible", async () => {
    renderApp("/governance/drift", {
      folders: [],
      permissions: ["governance:drift-read"],
    });
    expect(
      await screen.findByText("You cannot see any folder yet."),
    ).toBeVisible();
  });

  it("uses the first visible folder when ROOT is not readable", async () => {
    let asked: string | null = null;
    renderApp("/governance/drift", {
      folders: [acme],
      permissions: ["governance:drift-read"],
      handlers: [
        http.get("*/api/v1/contracts/governance/drift", ({ request }) => {
          asked = new URL(request.url).searchParams.get("folder_id");
          return HttpResponse.json(driftPage([], null));
        }),
      ],
    });
    expect(await screen.findByText("No drift")).toBeVisible();
    expect(asked).toBe("acme-id");
  });
});
