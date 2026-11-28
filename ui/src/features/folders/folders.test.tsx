// The folder explorer through the real router: the folders page, a folder's header, tabs and
// contract list (with Load more), creating a child folder, archiving, and the 404 state.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, makeMe, renderApp, rootFolder } from "@/test/renderApp";

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
const ledger = makeFolder({
  id: "ledger-id",
  parent_id: "payments-id",
  kind: "team",
  slug: "ledger",
  display_name: "Ledger",
  path: "acme/payments/ledger",
});
const folders = [rootFolder, acme, payments, ledger];

function contract(n: number) {
  return {
    id: `c-${String(n)}`,
    folder_id: "ledger-id",
    organization_id: "acme-id",
    odcs_id: `urn:orders:${String(n)}`,
    notation: "yaml",
    name: `Orders ${String(n)}`,
    status: "active",
    api_version: "v3.1.0",
    domain: "sales",
    tags: ["pii"],
    media_type: "application/yaml",
    content_sha256: null,
    size_bytes: null,
    content_redacted: false,
    template: null,
    origin: null,
    version: { number: 1, state: "latest", deprecated: false },
    latest_version: n === 1 ? { version_id: "v1", semver: "1.0.0" } : null,
    created_by: "local:admin",
    created_at: "2026-11-01T10:00:00Z",
    updated_by: "local:admin",
    updated_at: "2026-11-02T10:00:00Z",
  };
}

describe("folders page", () => {
  it("shows the tree and offers Create organization to a ROOT folder creator", async () => {
    renderApp("/folders", { folders });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Folders" }),
    ).toBeVisible();
    const tree = await screen.findByRole("treegrid", { name: "All folders" });
    expect(within(tree).getByRole("row", { name: "Acme" })).toBeVisible();
    expect(
      await screen.findByRole("button", { name: "Create organization" }),
    ).toBeVisible();
  });

  it("expands every folder with Expand all and closes all but Root with Collapse all", async () => {
    const user = userEvent.setup();
    renderApp("/folders", { folders });
    const tree = await screen.findByRole("treegrid", { name: "All folders" });
    const expandAll = screen.getByRole("button", { name: "Expand all" });
    const collapseAll = screen.getByRole("button", { name: "Collapse all" });
    expect(within(tree).queryByRole("row", { name: "Ledger" })).toBeNull();
    // Only Root is open: there is nothing to collapse yet.
    expect(expandAll).toBeEnabled();
    expect(collapseAll).toBeDisabled();

    await user.click(expandAll);
    expect(within(tree).getByRole("row", { name: "Payments" })).toBeVisible();
    expect(within(tree).getByRole("row", { name: "Ledger" })).toBeVisible();
    expect(within(tree).getByRole("row", { name: "Acme" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    // Everything is open: Expand all would change nothing.
    expect(expandAll).toBeDisabled();
    expect(collapseAll).toBeEnabled();

    await user.click(collapseAll);
    expect(within(tree).getByRole("row", { name: "Acme" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(within(tree).queryByRole("row", { name: "Payments" })).toBeNull();
    expect(within(tree).queryByRole("row", { name: "Ledger" })).toBeNull();
    expect(expandAll).toBeEnabled();
    expect(collapseAll).toBeDisabled();
  });

  it("enables Collapse all when one folder is opened by hand, and judges only what is on screen", async () => {
    const user = userEvent.setup();
    renderApp("/folders", { folders });
    const tree = await screen.findByRole("treegrid", { name: "All folders" });
    const expandAll = screen.getByRole("button", { name: "Expand all" });
    const collapseAll = screen.getByRole("button", { name: "Collapse all" });

    await user.click(within(tree).getByRole("row", { name: "Acme" }));
    expect(collapseAll).toBeEnabled();
    // Payments is still closed.
    expect(expandAll).toBeEnabled();
    await user.click(within(tree).getByRole("row", { name: "Payments" }));
    expect(expandAll).toBeDisabled();
    // Closing Acme hides Payments (still open underneath): the screen shows only Root open, so
    // Collapse all would change nothing visible.
    await user.click(within(tree).getByRole("row", { name: "Acme" }));
    expect(collapseAll).toBeDisabled();
    expect(expandAll).toBeEnabled();
  });

  it("disables both when no folder can open, and lets Collapse all reopen a closed Root", async () => {
    const user = userEvent.setup();
    renderApp("/folders", { folders: [rootFolder, acme] });
    const tree = await screen.findByRole("treegrid", { name: "All folders" });
    const expandAll = screen.getByRole("button", { name: "Expand all" });
    const collapseAll = screen.getByRole("button", { name: "Collapse all" });
    // Acme has no subfolders and contracts are not shown: it has no arrow.
    expect(within(tree).getByRole("row", { name: "Acme" })).not.toHaveAttribute(
      "aria-expanded",
    );
    expect(expandAll).toBeDisabled();
    expect(collapseAll).toBeDisabled();

    // Collapse all leaves Root open, so with Root closed by hand it would change the tree.
    await user.click(within(tree).getByRole("row", { name: "Root" }));
    expect(within(tree).queryByRole("row", { name: "Acme" })).toBeNull();
    expect(expandAll).toBeEnabled();
    expect(collapseAll).toBeEnabled();
    await user.click(collapseAll);
    expect(within(tree).getByRole("row", { name: "Acme" })).toBeVisible();
    expect(collapseAll).toBeDisabled();
  });

  it("with contracts shown, counts a folder whose empty list has loaded as unable to open", async () => {
    const user = userEvent.setup();
    renderApp("/folders?contracts=true", {
      folders: [rootFolder, acme],
      handlers: [
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
      ],
    });
    const tree = await screen.findByRole("treegrid", { name: "All folders" });
    const expandAll = screen.getByRole("button", { name: "Expand all" });
    const collapseAll = screen.getByRole("button", { name: "Collapse all" });
    // Acme's contracts are unknown until it opens, so it can open.
    expect(expandAll).toBeEnabled();
    await user.click(expandAll);
    const acmeRow = within(tree).getByRole("row", { name: "Acme" });
    await waitFor(() => {
      expect(acmeRow).not.toHaveAttribute("aria-expanded");
    });
    // Its list came back empty: it lost its arrow, so neither command would change anything.
    expect(expandAll).toBeDisabled();
    expect(collapseAll).toBeDisabled();
  });

  it("sizes the folder columns for folder content, so the Name track keeps room at 1024px", async () => {
    renderApp("/folders", { folders });
    const tree = await screen.findByRole("treegrid", { name: "All folders" });
    // Kind fits the widest kind badge (Organization), the counts a number: 23.5rem with Actions.
    expect(tree.style.getPropertyValue("--tree-cols")).toBe(
      "minmax(0, 1fr) 7rem 6rem 6rem 4.5rem",
    );
    expect(tree.style.getPropertyValue("--tree-cols-narrow")).toBe(
      "minmax(0, 1fr) 4.5rem",
    );
  });

  it("shows kind badges, counts and quick actions on navigator rows", async () => {
    const user = userEvent.setup();
    renderApp("/folders", {
      folders,
      handlers: [
        http.get("*/api/v1/contracts/contracts", ({ request }) => {
          const url = new URL(request.url);
          expect(url.searchParams.get("include_subtree")).toBe("true");
          const id = url.searchParams.get("folder_id");
          return HttpResponse.json({
            items: id === "acme-id" ? [contract(1), contract(2)] : [],
            next_cursor: null,
          });
        }),
      ],
    });
    const tree = await screen.findByRole("treegrid", { name: "All folders" });
    const acmeItem = within(tree).getByRole("row", { name: "Acme" });
    await waitFor(() => {
      expect(
        within(acmeItem)
          .getAllByRole("gridcell")
          .slice(1, 4)
          .map((cell) => cell.textContent),
      ).toEqual(["Organization", "1", "2"]);
    });
    // The row actions are real buttons in the row's actions gridcell.
    const acmeRow = acmeItem;
    expect(
      within(acmeRow).getByRole("link", { name: "Open Acme" }),
    ).toHaveAttribute("href", "/folders/acme-id");
    await user.click(
      await within(acmeRow).findByRole("button", {
        name: "Create project in Acme",
      }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Create project" }),
    ).toBeVisible();
  });

  it("shows an empty state without a create action to someone who cannot create", async () => {
    renderApp("/folders", {
      folders: [],
      me: makeMe({ assignments: [], global_permissions: [] }),
      permissions: [],
    });
    expect(
      await screen.findByRole("heading", { name: "No folders yet" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Create organization" }),
    ).toBeNull();
  });
});

describe("folder page", () => {
  it("switches folder from the header picker and keeps the tab", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/folders/payments-id/contracts", {
      folders,
      handlers: [
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
      ],
    });
    const picker = await screen.findByRole("combobox", {
      name: "Switch folder",
    });
    await waitFor(() => {
      expect(picker).toHaveValue("payments-id");
    });
    await user.selectOptions(picker, "ledger-id");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(
        "/folders/ledger-id/contracts",
      );
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Ledger" }),
    ).toBeVisible();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(
      await within(crumbs).findByRole("link", { name: "Payments" }),
    ).toHaveAttribute("href", "/folders/payments-id");
    expect(within(crumbs).getByRole("link", { name: "Acme" })).toBeVisible();
  });

  it("shows the header, breadcrumbs, badges and overview", async () => {
    renderApp("/folders/payments-id", { folders });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Payments" }),
    ).toBeVisible();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(crumbs).getByRole("link", { name: "Folders" }),
    ).toHaveAttribute("href", "/folders");
    expect(
      await within(crumbs).findByRole("link", { name: "Acme" }),
    ).toHaveAttribute("href", "/folders/acme-id");
    expect(within(crumbs).getByText("Payments")).toHaveAttribute(
      "aria-current",
      "page",
    );
    const tabs = screen.getByRole("navigation", { name: "Folder sections" });
    expect(
      within(tabs).getByRole("link", { name: "Overview" }),
    ).toHaveAttribute("aria-current", "page");
    expect(within(tabs).getByRole("link", { name: "Folders" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Details" })).toBeVisible();
    expect(
      await screen.findByRole("button", { name: "Create team" }),
    ).toBeVisible();
  });

  it("lists a team's contracts and loads more with the cursor", async () => {
    const user = userEvent.setup();
    const seen: (string | null)[] = [];
    renderApp("/folders/ledger-id/contracts", {
      folders,
      handlers: [
        http.get("*/api/v1/contracts/contracts", ({ request }) => {
          const url = new URL(request.url);
          expect(url.searchParams.get("folder_id")).toBe("ledger-id");
          const cursor = url.searchParams.get("cursor");
          seen.push(cursor);
          return HttpResponse.json(
            cursor === null
              ? { items: [contract(1)], next_cursor: "next-1" }
              : { items: [contract(2)], next_cursor: null },
          );
        }),
      ],
    });
    const table = await screen.findByRole("table", {
      name: "Contracts in Ledger",
    });
    const first = within(table).getByRole("link", { name: "Orders 1" });
    expect(first).toHaveAttribute("href", "/contracts/c-1");
    expect(within(table).getByText("1.0.0")).toBeVisible();
    expect(screen.getByText("Showing 1 loaded")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(
      await within(table).findByRole("link", { name: "Orders 2" }),
    ).toBeVisible();
    expect(screen.getByText("All results loaded")).toBeVisible();
    expect(within(table).getByText("Not published")).toBeVisible();
    expect(seen).toEqual([null, "next-1"]);
  });

  it("creates a child folder with a suggested slug and opens it", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    const created = makeFolder({
      id: "fraud-id",
      parent_id: "payments-id",
      kind: "team",
      slug: "fraud-ops",
      display_name: "Fraud Ops",
      path: "acme/payments/fraud-ops",
    });
    const { router } = renderApp("/folders/payments-id", {
      folders: [...folders, created],
      handlers: [
        http.post("*/api/v1/identity/folders", async ({ request }) => {
          body = await request.json();
          return HttpResponse.json(created, { status: 201 });
        }),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create team" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create team" });
    await user.type(within(dialog).getByLabelText("Name"), "Fraud Ops");
    expect(within(dialog).getByLabelText("Slug")).toHaveValue("fraud-ops");
    await user.click(
      within(dialog).getByRole("button", { name: "Create team" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders/fraud-id");
    });
    expect(body).toEqual({
      parent_id: "payments-id",
      slug: "fraud-ops",
      display_name: "Fraud Ops",
    });
  });

  it("shows the name rule message in the create dialog and sends nothing", async () => {
    const user = userEvent.setup();
    let called = false;
    renderApp("/folders/payments-id", {
      folders,
      handlers: [
        http.post("*/api/v1/identity/folders", () => {
          called = true;
          return HttpResponse.json({}, { status: 201 });
        }),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create team" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create team" });
    await user.type(within(dialog).getByLabelText("Name"), "Ops 😀");
    expect(
      within(dialog).getByText(
        "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.",
      ),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole("button", { name: "Create team" }),
    );
    expect(called).toBe(false);
  });

  it("shows the name rule message in the rename dialog and sends nothing", async () => {
    const user = userEvent.setup();
    let called = false;
    renderApp("/folders/payments-id", {
      folders,
      handlers: [
        http.patch("*/api/v1/identity/folders/:id", () => {
          called = true;
          return HttpResponse.json(payments);
        }),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Actions for Payments" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Rename folder" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Rename Payments",
    });
    await user.type(within(dialog).getByLabelText("Name"), " 😀");
    expect(
      within(dialog).getByText(
        "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.",
      ),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole("button", { name: "Rename folder" }),
    );
    expect(called).toBe(false);
  });

  it("archives through the confirmation dialog", async () => {
    const user = userEvent.setup();
    let archived = false;
    renderApp("/folders/payments-id", {
      folders,
      handlers: [
        http.post("*/api/v1/identity/folders/:id/archive", () => {
          archived = true;
          return HttpResponse.json({ ...payments, archived: true });
        }),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Actions for Payments" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Archive folder" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Archive Payments?",
    });
    expect(
      within(dialog).getByRole("button", { name: "Keep Payments" }),
    ).toHaveFocus();
    await user.click(
      within(dialog).getByRole("button", { name: "Archive Payments" }),
    );
    await waitFor(() => {
      expect(archived).toBe(true);
    });
  });

  it("hides write actions without the capabilities", async () => {
    renderApp("/folders/payments-id", {
      folders,
      permissions: ["folder:read"],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Payments" }),
    ).toBeVisible();
    await screen.findByRole("heading", { name: "Details" });
    expect(screen.queryByRole("button", { name: "Create team" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Actions for Payments" }),
    ).toBeNull();
  });

  it("renders Not found for an unknown or hidden folder", async () => {
    renderApp("/folders/missing-id", { folders });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Not found" }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "This item does not exist, or you do not have access to it.",
      ),
    ).toBeVisible();
  });
});
