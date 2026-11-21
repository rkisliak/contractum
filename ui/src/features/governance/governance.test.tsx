// Dry-run and drift through the real router: results tables link to contracts, summaries add
// up, filters narrow loaded rows and metadata-only callers see "Details withheld".
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ImpactFailingRule } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder } from "@/test/renderApp";

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

function failing(key: string, withheld = false): ImpactFailingRule {
  return {
    rule_key: key,
    rule_set_id: "rs-1",
    version_no: 1,
    severity: "error",
    mode: "warn",
    outcome: "fail",
    blocking: true,
    error_code: null,
    message: "Add a description.",
    pointers: [],
    pointers_total: 0,
    pointers_withheld: withheld,
  };
}

describe("dry-run", () => {
  it("runs from the URL and lists affected contracts with links", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    renderApp(
      "/governance/dry-run?rule_set_id=rs-1&version=1&folder_id=acme-id",
      {
        folders,
        handlers: [
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
              body = await request.json();
              return HttpResponse.json({
                rule_set_id: "rs-1",
                version: 1,
                scope_folder_id: "acme-id",
                total_in_scope: 2,
                next_cursor: null,
                summary: { would_fail: 1, would_error: 0, pass: 1, hidden: 0 },
                items: [
                  {
                    contract_id: "c-1",
                    folder_id: "acme-id",
                    name: "Orders",
                    odcs_id: "urn:orders",
                    latest_semver: "1.0.0",
                    status: "evaluated",
                    would_block: true,
                    counts: { fail: 1, error: 0, warn: 0 },
                    failing_rules: [failing("require-description", true)],
                  },
                  {
                    contract_id: "c-2",
                    folder_id: "acme-id",
                    name: "Refunds",
                    odcs_id: "urn:refunds",
                    latest_semver: "2.0.0",
                    status: "evaluated",
                    would_block: false,
                    counts: { fail: 0, error: 0, warn: 1 },
                    failing_rules: [
                      { ...failing("domain-set"), blocking: false },
                    ],
                  },
                ],
              });
            },
          ),
        ],
      },
    );
    expect(await screen.findByRole("link", { name: "Orders" })).toHaveAttribute(
      "href",
      "/contracts/c-1",
    );
    expect(body).toEqual({
      rule_set_id: "rs-1",
      version: 1,
      folder_id: "acme-id",
    });
    expect(screen.getByText("2 contracts in scope")).toBeVisible();
    expect(screen.getByText("Would block")).toBeVisible();
    expect(
      screen.getByText(
        "Details withheld: you do not have contract access here",
      ),
    ).toBeVisible();
    expect(screen.getByText("Would fail")).toBeVisible();

    await user.click(
      screen.getByRole("checkbox", { name: "Only would block" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "Refunds" })).toBeNull();
    });
  });
});

describe("drift", () => {
  it("shows a calm note instead of requesting drift without governance:drift-read", async () => {
    let requested = false;
    renderApp("/governance/drift?folder_id=acme-id", {
      folders,
      permissions: ["folder:read"],
      handlers: [
        http.get("*/api/v1/contracts/governance/drift", () => {
          requested = true;
          return HttpResponse.json({}, { status: 403 });
        }),
      ],
    });
    expect(
      await screen.findByRole("heading", {
        name: "Not available at your access level",
      }),
    ).toBeVisible();
    expect(requested).toBe(false);
  });

  it("shows the drift report for the chosen folder", async () => {
    let folder: string | null = null;
    renderApp("/governance/drift?folder_id=acme-id", {
      folders,
      permissions: ["governance:drift-read"],
      handlers: [
        http.get("*/api/v1/contracts/governance/drift", ({ request }) => {
          folder = new URL(request.url).searchParams.get("folder_id");
          return HttpResponse.json({
            folder_id: "acme-id",
            total_in_scope: 3,
            next_cursor: null,
            summary: {
              drifted: 1,
              at_risk: 0,
              compliant: 2,
              unparseable: 0,
              hidden: 0,
            },
            items: [
              {
                contract_id: "c-1",
                folder_id: "acme-id",
                name: "Orders",
                odcs_id: "urn:orders",
                latest_semver: "1.0.0",
                status: "drifted",
                counts: { fail: 1, error: 0, warn: 0 },
                failing_rules: [failing("require-description")],
                pinned_rule_sets: [],
                current_versions: [
                  {
                    rule_set_id: "rs-1",
                    name: "acme-baseline",
                    version_no: 1,
                    mode: "warn",
                  },
                ],
              },
            ],
          });
        }),
      ],
    });
    const link = await screen.findByRole("link", { name: "Orders" });
    expect(link).toHaveAttribute("href", "/contracts/c-1");
    expect(folder).toBe("acme-id");
    const row = link.closest("tr") as HTMLElement;
    expect(within(row).getByText("Drifted")).toBeVisible();
    expect(within(row).getByText(/acme-baseline v1 \(warn\)/)).toBeVisible();
    expect(screen.getByText("3 contracts in scope")).toBeVisible();
  });
});
