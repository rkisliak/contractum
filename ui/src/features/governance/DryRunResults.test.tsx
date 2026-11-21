// The dry-run results on their own: every outcome badge, the name and version fallbacks, sorting,
// paging with the cursor (and a failed next page), a failed first page with a retry, and an
// answer without a body.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { describe, expect, it } from "vitest";
import type { DryRunItem } from "@/api/types";
import { renderWithApi } from "@/test/governance-hooks";
import { sortRowsBy } from "@/test/governance-table";
import { http, HttpResponse, server } from "@/test/msw";
import { DryRunResults } from "./DryRunResults";

const DRY_RUN = "*/api/v1/contracts/governance/rule-set-dry-runs";

function item(id: string, overrides: Partial<DryRunItem> = {}): DryRunItem {
  return {
    contract_id: id,
    folder_id: "acme-id",
    name: id,
    odcs_id: `${id}-odcs`,
    latest_semver: "1.0.0",
    status: "evaluated",
    would_block: false,
    counts: { fail: 0, error: 0, warn: 0 },
    failing_rules: [],
    ...overrides,
  };
}

function result(items: DryRunItem[], next: string | null = null) {
  return {
    rule_set_id: "rs-1",
    version: 2,
    scope_folder_id: null,
    total_in_scope: 1,
    next_cursor: next,
    summary: { would_fail: 1, would_error: 0, pass: 1, hidden: 0 },
    items,
  };
}

function problem(status: number) {
  return HttpResponse.json(
    { type: "urn:dc:problem:broken", title: "Dry run broke", status },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

function mount(handler: RequestHandler) {
  server.use(
    http.get("*/api/v1/identity/folders", () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    ),
    handler,
  );
  renderWithApi(
    <DryRunResults request={{ ruleSetId: "rs-1", version: 2, run: 1 }} />,
  );
}

describe("dry-run results", () => {
  it("badges every outcome, falls back to the ODCS id and sorts by contract", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    mount(
      http.post(DRY_RUN, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(
          result([
            item("b-pass"),
            item("c-broken", { status: "unparseable" }),
            item("a-blocked", { name: null, would_block: true }),
            item("d-warn", {
              latest_semver: null,
              failing_rules: [
                {
                  rule_key: "has-owner",
                  rule_set_id: "rs-1",
                  version_no: 2,
                  severity: "warning",
                  mode: "warn",
                  outcome: "fail",
                  blocking: false,
                  error_code: null,
                  message: "Name an owner.",
                  pointers: [],
                  pointers_total: 0,
                  pointers_withheld: false,
                },
              ],
            }),
          ]),
        );
      }),
    );
    const table = await screen.findByRole("table", { name: "Dry-run results" });
    expect(screen.getByText("1 contract in scope")).toBeVisible();
    // A whole-scope run sends no folder and no cursor.
    expect(bodies).toEqual([{ rule_set_id: "rs-1", version: 2 }]);
    const rowOf = (name: string) =>
      within(
        within(table).getByRole("link", { name }).closest("tr") as HTMLElement,
      );
    expect(rowOf("b-pass").getByText("Passes")).toBeVisible();
    expect(rowOf("c-broken").getByText("Unparseable")).toBeVisible();
    expect(rowOf("a-blocked-odcs").getByText("Would block")).toBeVisible();
    expect(rowOf("d-warn").getByText("Warnings")).toBeVisible();
    expect(rowOf("d-warn").getByText("Not published")).toBeVisible();
    // The filters narrow the loaded rows and "Any" lifts them again.
    await user.selectOptions(
      screen.getByLabelText("Failing rule"),
      "has-owner",
    );
    expect(within(table).getAllByRole("rowheader")).toHaveLength(1);
    await user.selectOptions(screen.getByLabelText("Failing rule"), "");
    await user.selectOptions(screen.getByLabelText("Folder"), "acme-id");
    await user.selectOptions(screen.getByLabelText("Folder"), "");
    expect(within(table).getAllByRole("rowheader")).toHaveLength(4);
    expect(await sortRowsBy(user, table, "Contract and outcome")).toEqual([
      expect.stringContaining("a-blocked-odcs"),
      expect.stringContaining("b-pass"),
      expect.stringContaining("c-broken"),
      expect.stringContaining("d-warn"),
    ]);
  });

  it("loads the next page with the cursor and keeps the rows when it fails", async () => {
    const user = userEvent.setup();
    const cursors: unknown[] = [];
    mount(
      http.post(DRY_RUN, async ({ request }) => {
        const body = (await request.json()) as { cursor?: string };
        cursors.push(body.cursor ?? null);
        if (body.cursor === "c2")
          return HttpResponse.json(result([item("second")], "c3"));
        if (body.cursor === "c3") return problem(500);
        return HttpResponse.json(result([item("first")], "c2"));
      }),
    );
    await screen.findByRole("link", { name: "first" });
    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(await screen.findByRole("link", { name: "second" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Load more results" }));
    await waitFor(() => {
      expect(cursors).toEqual([null, "c2", "c3"]);
    });
    expect(screen.getByRole("link", { name: "first" })).toBeVisible();
    expect(await screen.findByText("Dry run broke")).toBeVisible();
  });

  it("retries a failed first page", async () => {
    const user = userEvent.setup();
    let calls = 0;
    mount(
      http.post(DRY_RUN, () => {
        calls += 1;
        return calls === 1
          ? problem(500)
          : HttpResponse.json(result([item("first")]));
      }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Retry loading" }),
    );
    expect(await screen.findByRole("link", { name: "first" })).toBeVisible();
  });

  it("treats an answer without a body as a failure", async () => {
    mount(http.post(DRY_RUN, () => new HttpResponse(null, { status: 204 })));
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });
});
