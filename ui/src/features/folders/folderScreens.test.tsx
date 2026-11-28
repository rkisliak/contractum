// Folder screens through the real router: renaming, unarchiving, the create-folder dialog's
// validation and error paths, the Folders tab, the Contracts tab's filters and states, the
// folders page error and archived views, and the header's folder switcher.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import {
  makeFolder,
  makeMe,
  renderApp,
  ROOT_ID,
  rootFolder,
} from "@/test/renderApp";

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
const fraud = makeFolder({
  id: "fraud-id",
  parent_id: "payments-id",
  kind: "team",
  slug: "fraud",
  display_name: "Fraud",
  path: "acme/payments/fraud",
  archived: true,
  archived_at: "2026-11-03T10:00:00Z",
});
const folders = [rootFolder, acme, payments, ledger];

function problem(status: number, body: Record<string, unknown>) {
  return HttpResponse.json(
    { status, ...body },
    {
      status,
      headers: { "Content-Type": "application/problem+json" },
    },
  );
}

function contractRow(overrides: Record<string, unknown> = {}) {
  return {
    id: "c-1",
    folder_id: "ledger-id",
    organization_id: "acme-id",
    odcs_id: "urn:orders:1",
    notation: "yaml",
    name: "Orders",
    status: "active",
    api_version: "v3.1.0",
    domain: "sales",
    tags: ["pii", "finance"],
    media_type: "application/yaml",
    content_sha256: null,
    size_bytes: null,
    content_redacted: false,
    template: null,
    origin: null,
    version: { number: 1, state: "latest", deprecated: false },
    latest_version: { version_id: "v1", semver: "1.2.0" },
    created_by: "local:admin",
    created_at: "2026-11-01T10:00:00Z",
    updated_by: "local:admin",
    updated_at: "2026-11-02T10:00:00Z",
    ...overrides,
  };
}

async function openMenuItem(folderName: string, item: string) {
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole(
      "button",
      { name: `Actions for ${folderName}` },
      { timeout: 10_000 },
    ),
  );
  await user.click(await screen.findByRole("menuitem", { name: item }));
  return user;
}

describe("rename folder", () => {
  it("sends the trimmed display name and closes on success", async () => {
    let sent: { method: string; path: string; body: unknown } | null = null;
    renderApp("/folders/payments-id", {
      folders,
      handlers: [
        http.patch("*/api/v1/identity/folders/:id", async ({ request }) => {
          sent = {
            method: request.method,
            path: new URL(request.url).pathname,
            body: await request.json(),
          };
          return HttpResponse.json({ ...payments, display_name: "Cards" });
        }),
      ],
    });
    const user = await openMenuItem("Payments", "Rename folder");
    const dialog = await screen.findByRole("dialog", {
      name: "Rename Payments",
    });
    expect(
      within(dialog).getByText(
        "Only the display name changes. The slug and the folder path stay the same.",
      ),
    ).toBeVisible();
    const field = within(dialog).getByLabelText("Name");
    expect(field).toHaveValue("Payments");
    await user.clear(field);
    await user.type(field, "  Cards  ");
    await user.click(
      within(dialog).getByRole("button", { name: "Rename folder" }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Rename Payments" }),
      ).toBeNull();
    });
    expect(sent).toEqual({
      method: "PATCH",
      path: "/api/v1/identity/folders/payments-id",
      body: { display_name: "Cards" },
    });
  });

  it("asks for a name, shows a field error from the API and a ProblemAlert otherwise", async () => {
    let attempt = 0;
    renderApp("/folders/payments-id", {
      folders,
      handlers: [
        http.patch("*/api/v1/identity/folders/:id", () => {
          attempt += 1;
          return attempt === 1
            ? problem(422, {
                type: "urn:dc:problem:validation-error",
                title: "Validation failed",
                errors: [
                  { pointer: "/display_name", detail: "Name is too fancy." },
                ],
              })
            : problem(500, {
                type: "urn:dc:problem:internal",
                title: "Something broke",
              });
        }),
      ],
    });
    const user = await openMenuItem("Payments", "Rename folder");
    const dialog = await screen.findByRole("dialog", {
      name: "Rename Payments",
    });
    const field = within(dialog).getByLabelText("Name");
    await user.clear(field);
    await user.type(field, "   ");
    const submit = within(dialog).getByRole("button", {
      name: "Rename folder",
    });
    await user.click(submit);
    expect(
      await within(dialog).findByText("Enter a name for the folder."),
    ).toBeVisible();
    expect(attempt).toBe(0);

    await user.clear(field);
    await user.type(field, "Fancy");
    await user.click(submit);
    expect(await within(dialog).findByText("Name is too fancy.")).toBeVisible();
    // A mapped field error stays on the field; no ProblemAlert headline.
    expect(within(dialog).queryByText("Validation failed")).toBeNull();

    await user.click(submit);
    expect(await within(dialog).findByText("Something broke")).toBeVisible();
    expect(within(dialog).queryByText("Name is too fancy.")).toBeNull();
    expect(attempt).toBe(2);
  });
});

describe("rename folder cancel paths", () => {
  it("Keep current name and Escape both close without a request", async () => {
    let requests = 0;
    renderApp("/folders/payments-id", {
      folders,
      handlers: [
        http.patch("*/api/v1/identity/folders/:id", () => {
          requests += 1;
          return HttpResponse.json(payments);
        }),
      ],
    });
    const user = await openMenuItem("Payments", "Rename folder");
    let dialog = await screen.findByRole("dialog", { name: "Rename Payments" });
    await user.clear(within(dialog).getByLabelText("Name"));
    await user.type(within(dialog).getByLabelText("Name"), "Draft name");
    await user.click(
      within(dialog).getByRole("button", { name: "Keep current name" }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Rename Payments" }),
      ).toBeNull();
    });

    await user.click(
      screen.getByRole("button", { name: "Actions for Payments" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Rename folder" }),
    );
    dialog = await screen.findByRole("dialog", { name: "Rename Payments" });
    expect(within(dialog).getByLabelText("Name")).toHaveValue("Payments");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Rename Payments" }),
      ).toBeNull();
    });
    expect(requests).toBe(0);
  });
});

describe("archived folder", () => {
  it("shows the banner and offers Unarchive (and Delete), which posts to the API", async () => {
    let unarchived: string | null = null;
    renderApp("/folders/fraud-id", {
      folders: [...folders, fraud],
      handlers: [
        http.post("*/api/v1/identity/folders/:id/unarchive", ({ request }) => {
          unarchived = new URL(request.url).pathname;
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Fraud" }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "This folder is archived. It and everything below it are read-only. Reads keep working.",
      ),
    ).toBeVisible();
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Actions for Fraud" }),
    );
    expect(
      screen.getAllByRole("menuitem").map((item) => item.textContent),
    ).toEqual(["Unarchive folder", "Delete folder…"]);
    await user.click(
      screen.getByRole("menuitem", { name: "Unarchive folder" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Unarchive Fraud?",
    });
    expect(
      within(dialog).getByRole("button", { name: "Go back" }),
    ).toHaveFocus();
    await user.click(
      within(dialog).getByRole("button", { name: "Unarchive Fraud" }),
    );
    await waitFor(() => {
      expect(unarchived).toBe("/api/v1/identity/folders/fraud-id/unarchive");
    });
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("keeps the archive dialog open with a ProblemAlert when archiving fails", async () => {
    renderApp("/folders/payments-id", {
      folders,
      handlers: [
        http.post("*/api/v1/identity/folders/:id/archive", () =>
          problem(409, {
            type: "urn:dc:problem:conflict",
            title: "Folder has active subscriptions",
          }),
        ),
      ],
    });
    const user = await openMenuItem("Payments", "Archive folder");
    const dialog = await screen.findByRole("alertdialog", {
      name: "Archive Payments?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Archive Payments" }),
    );
    expect(
      await within(dialog).findByText("Folder has active subscriptions"),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Keep Payments" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });
});

describe("root folder header", () => {
  it("has no path, no actions menu and offers Create organization", async () => {
    renderApp(`/folders/${ROOT_ID}`, { folders });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Root" }),
    ).toBeVisible();
    expect(
      await screen.findByRole("button", { name: "Create organization" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Actions for Root" }),
    ).toBeNull();
    // ROOT has no Subscribers tab.
    const tabs = screen.getByRole("navigation", { name: "Folder sections" });
    expect(
      within(tabs).queryByRole("link", { name: "Subscribers" }),
    ).toBeNull();
  });

  it("shows a ProblemAlert with retry when the folder fails to load", async () => {
    let calls = 0;
    renderApp("/folders/acme-id", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/folders/:id", () => {
          calls += 1;
          return calls === 1
            ? problem(500, {
                type: "urn:dc:problem:internal",
                title: "Folder service down",
              })
            : HttpResponse.json(acme);
        }),
      ],
    });
    expect(await screen.findByText("Folder service down")).toBeVisible();
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Acme" }),
    ).toBeVisible();
  });
});

describe("folder switcher", () => {
  it("goes to the target's Overview when it lacks the current tab", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/folders/payments-id/folders", { folders });
    const picker = await screen.findByRole("combobox", {
      name: "Switch folder",
    });
    await waitFor(() => {
      expect(picker).toHaveValue("payments-id");
    });
    // A team has no Folders tab.
    await user.selectOptions(picker, "ledger-id");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders/ledger-id");
    });
  });

  it("goes to the target's Overview from an Overview", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/folders/payments-id", { folders });
    const picker = await screen.findByRole("combobox", {
      name: "Switch folder",
    });
    await waitFor(() => {
      expect(picker).toHaveValue("payments-id");
    });
    await user.selectOptions(picker, "acme-id");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders/acme-id");
    });
  });

  it("ignores the placeholder", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/folders/payments-id/contracts", {
      folders,
    });
    const picker = await screen.findByRole("combobox", {
      name: "Switch folder",
    });
    await waitFor(() => {
      expect(picker).toHaveValue("payments-id");
    });
    await user.selectOptions(picker, "");
    expect(router.state.location.pathname).toBe(
      "/folders/payments-id/contracts",
    );
  });
});

describe("create folder dialog", () => {
  it("validates the name and slug, keeps an edited slug and maps a slug conflict", async () => {
    const bodies: unknown[] = [];
    renderApp("/folders/payments-id", {
      folders,
      handlers: [
        http.post("*/api/v1/identity/folders", async ({ request }) => {
          bodies.push(await request.json());
          return problem(409, {
            type: "urn:dc:problem:conflict",
            title: "Conflict",
          });
        }),
      ],
    });
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Create team" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create team" });
    expect(
      within(dialog).getByText("The new team is created inside Payments."),
    ).toBeVisible();
    expect(within(dialog).getByText("Team")).toBeVisible();
    const submit = within(dialog).getByRole("button", { name: "Create team" });
    await user.click(submit);
    expect(
      within(dialog).getByText("Enter a name for the team."),
    ).toBeVisible();
    expect(
      within(dialog).getByText(
        "Use lowercase letters, digits and single hyphens.",
      ),
    ).toBeVisible();
    expect(bodies).toEqual([]);

    const slug = within(dialog).getByLabelText("Slug");
    await user.type(slug, "risk");
    await user.type(within(dialog).getByLabelText("Name"), "Risk Team");
    // An edited slug is no longer suggested from the name.
    expect(slug).toHaveValue("risk");
    await user.click(submit);
    expect(
      await within(dialog).findByText(
        "A folder with this slug already exists here.",
      ),
    ).toBeVisible();
    expect(bodies).toEqual([
      { parent_id: "payments-id", slug: "risk", display_name: "Risk Team" },
    ]);
  });

  it("maps 422 field errors and shows other problems in an alert", async () => {
    let attempt = 0;
    renderApp("/folders/payments-id", {
      folders,
      handlers: [
        http.post("*/api/v1/identity/folders", () => {
          attempt += 1;
          return attempt === 1
            ? problem(422, {
                type: "urn:dc:problem:validation-error",
                title: "Validation failed",
                errors: [
                  { pointer: "/display_name", detail: "Name taken." },
                  { pointer: "/slug", detail: "Slug reserved." },
                ],
              })
            : problem(403, {
                type: "urn:dc:problem:forbidden",
                title: "Forbidden",
                detail: "You need folder:create here.",
              });
        }),
      ],
    });
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Create team" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create team" });
    await user.type(within(dialog).getByLabelText("Name"), "Admin");
    const submit = within(dialog).getByRole("button", { name: "Create team" });
    await user.click(submit);
    // Each message sits on its field and in the alert list under the field's label.
    await waitFor(() => {
      expect(within(dialog).getAllByText("Name taken.")).toHaveLength(2);
    });
    expect(within(dialog).getAllByText("Slug reserved.")).toHaveLength(2);
    const alert = within(dialog)
      .getByText("Validation failed")
      .closest("[role='alert']") as HTMLElement;
    expect(within(alert).getByText("Validation failed")).toBeVisible();
    expect(within(alert).getByText("Name")).toBeVisible();
    expect(within(alert).getByText("Slug")).toBeVisible();
    await user.click(submit);
    expect(
      await within(dialog).findByText("You need folder:create here."),
    ).toBeVisible();
    expect(within(dialog).getByRole("alert")).toBeVisible();
  });

  it("forgets what was typed after Go back", async () => {
    renderApp("/folders/payments-id", { folders });
    const user = userEvent.setup();
    const open = await screen.findByRole("button", { name: "Create team" });
    await user.click(open);
    let dialog = await screen.findByRole("dialog", { name: "Create team" });
    await user.type(within(dialog).getByLabelText("Name"), "Temp");
    expect(within(dialog).getByLabelText("Slug")).toHaveValue("temp");
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Create team" })).toBeNull();
    });
    await user.click(open);
    dialog = await screen.findByRole("dialog", { name: "Create team" });
    expect(within(dialog).getByLabelText("Name")).toHaveValue("");
    expect(within(dialog).getByLabelText("Slug")).toHaveValue("");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Create team" })).toBeNull();
    });
  });
});

describe("folders tab", () => {
  it("lists the direct children by name with their status", async () => {
    const audit = makeFolder({
      id: "audit-id",
      parent_id: "payments-id",
      kind: "team",
      slug: "audit",
      display_name: "Audit",
      path: "acme/payments/audit",
    });
    renderApp("/folders/payments-id/folders", {
      folders: [...folders, fraud, audit],
    });
    const table = await screen.findByRole("table", {
      name: "Folders in Payments",
    });
    expect(
      within(table)
        .getAllByRole("link")
        .map((link) => [link.textContent, link.getAttribute("href")]),
    ).toEqual([
      ["Audit", "/folders/audit-id"],
      ["Fraud", "/folders/fraud-id"],
      ["Ledger", "/folders/ledger-id"],
    ]);
    const fraudRow = within(table)
      .getByRole("link", { name: "Fraud" })
      .closest("tr") as HTMLElement;
    expect(within(fraudRow).getByText("Archived")).toBeVisible();
    expect(within(fraudRow).getByText("acme/payments/fraud")).toBeVisible();
    expect(within(fraudRow).getByText("Team")).toBeVisible();
    const ledgerRow = within(table)
      .getByRole("link", { name: "Ledger" })
      .closest("tr") as HTMLElement;
    expect(within(ledgerRow).getByText("Active")).toBeVisible();
  });

  it("offers one create action, the folder header's, and opens the dialog", async () => {
    const user = userEvent.setup();
    renderApp("/folders/acme-id/folders", { folders: [rootFolder, acme] });
    expect(
      await screen.findByText(
        "Project folders created inside Acme appear here.",
      ),
    ).toBeVisible();
    const section = screen.getByRole("region", { name: "Folders" });
    // The empty state has no button of its own: the header's is the only one.
    expect(within(section).queryByRole("button")).toBeNull();
    expect(
      screen.getAllByRole("button", { name: "Create project" }),
    ).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(
      await screen.findByRole("dialog", { name: "Create project" }),
    ).toBeVisible();
  });

  it("has no create action without folder:create", async () => {
    renderApp("/folders/acme-id/folders", {
      folders: [rootFolder, acme],
      permissions: ["folder:read"],
    });
    const section = await screen.findByRole("region", { name: "Folders" });
    expect(
      await within(section).findByRole("heading", { name: "No folders yet" }),
    ).toBeVisible();
    expect(within(section).queryByRole("button")).toBeNull();
  });

  it("explains that a team holds no folders", async () => {
    renderApp("/folders/ledger-id/folders", { folders });
    expect(
      await screen.findByText("Team folders have no folders inside them."),
    ).toBeVisible();
  });

  it("shows a ProblemAlert when the folder list fails and retries", async () => {
    let failing = true;
    renderApp("/folders/acme-id/folders", {
      folders: [rootFolder, acme],
      handlers: [
        http.get("*/api/v1/identity/folders", ({ request }) => {
          const archived =
            new URL(request.url).searchParams.get("include_archived") ===
            "true";
          if (archived && failing)
            return problem(503, {
              type: "urn:dc:problem:unavailable",
              title: "Unavailable",
            });
          return HttpResponse.json({
            items: [rootFolder, acme],
            next_cursor: null,
          });
        }),
      ],
    });
    const retry = await screen.findByRole(
      "button",
      { name: "Retry loading" },
      { timeout: 10_000 },
    );
    expect(screen.getByText("Try again in a moment.")).toBeVisible();
    failing = false;
    await userEvent.setup().click(retry);
    const section = await screen.findByRole("region", { name: "Folders" });
    expect(
      within(section).getByRole("heading", { name: "No folders yet" }),
    ).toBeVisible();
  });
});

describe("contracts tab", () => {
  it("searches on submit, shows the filtered empty state and clears it", async () => {
    const queries: (string | null)[] = [];
    const { router } = renderApp("/folders/ledger-id/contracts", {
      folders,
      handlers: [
        http.get("*/api/v1/contracts/contracts", ({ request }) => {
          const params = new URL(request.url).searchParams;
          queries.push(params.get("q"));
          expect(params.get("include_subtree")).toBe("false");
          return HttpResponse.json({
            items: params.get("q") ? [] : [contractRow()],
            next_cursor: null,
          });
        }),
      ],
    });
    const user = userEvent.setup();
    const table = await screen.findByRole("table", {
      name: "Contracts in Ledger",
    });
    const orders = within(table)
      .getByRole("link", { name: "Orders" })
      .closest("tr") as HTMLElement;
    expect(within(orders).getByText("sales")).toBeVisible();
    expect(within(orders).getByText("active")).toBeVisible();
    expect(within(orders).getByText("pii")).toBeVisible();
    expect(within(orders).getByText("finance")).toBeVisible();
    expect(within(orders).getByText("1.2.0")).toBeVisible();
    // A team has no "Include subfolders" switch.
    expect(screen.queryByLabelText("Include subfolders")).toBeNull();

    const search = within(
      screen.getByRole("region", { name: "Contracts" }),
    ).getByRole("searchbox", { name: "Search contracts" });
    await user.type(search, "  zzz  {Enter}");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ q: "zzz" });
    });
    expect(
      await screen.findByRole("heading", {
        name: "No contracts match these filters.",
      }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    expect(search).toHaveValue("");
    expect(
      await within(
        await screen.findByRole("table", { name: "Contracts in Ledger" }),
      ).findByRole("link", { name: "Orders" }),
    ).toBeVisible();
    expect(queries).toContain("zzz");
  });

  it("applies the debounced search after typing stops", async () => {
    const { router } = renderApp("/folders/ledger-id/contracts", {
      folders,
      handlers: [
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
      ],
    });
    const user = userEvent.setup();
    await user.type(
      within(
        await screen.findByRole("region", { name: "Contracts" }),
      ).getByRole("searchbox", { name: "Search contracts" }),
      "ord",
    );
    await waitFor(
      () => {
        expect(router.state.location.search).toEqual({ q: "ord" });
      },
      { timeout: 5000 },
    );
  });

  it("toggles Include subfolders above a team and renders unset fields", async () => {
    const subtree: (string | null)[] = [];
    const { router } = renderApp("/folders/payments-id/contracts", {
      folders,
      handlers: [
        http.get("*/api/v1/contracts/contracts", ({ request }) => {
          subtree.push(
            new URL(request.url).searchParams.get("include_subtree"),
          );
          return HttpResponse.json({
            items: [
              contractRow({
                name: null,
                domain: null,
                status: null,
                tags: [],
                latest_version: null,
              }),
            ],
            next_cursor: null,
          });
        }),
      ],
    });
    const user = userEvent.setup();
    const table = await screen.findByRole("table", {
      name: "Contracts in Payments",
    });
    const row = within(table)
      .getByRole("link", { name: "urn:orders:1" })
      .closest("tr") as HTMLElement;
    expect(within(row).getAllByText("Not set")).toHaveLength(3);
    expect(within(row).getByText("Not published")).toBeVisible();
    const box = screen.getByRole("checkbox", { name: "Include subfolders" });
    expect(box).toBeChecked();
    // No contract:create here, so no New contract.
    expect(screen.queryByRole("button", { name: "New contract" })).toBeNull();
    await user.click(box);
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ subtree: false });
    });
    await waitFor(() => {
      expect(subtree).toEqual(["true", "false"]);
    });
  });

  it("explains where contracts live in an empty project and in an empty team", async () => {
    renderApp("/folders/payments-id/contracts", { folders });
    expect(
      await screen.findByText(
        "Contracts in this folder and the folders below it appear here.",
      ),
    ).toBeVisible();
  });

  it("offers New contract in a project with contract:create", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/folders/payments-id/contracts", {
      folders,
      permissions: ["contract:create"],
    });
    await user.click(
      await screen.findByRole("button", { name: "New contract" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/new");
    });
    expect(router.state.location.search).toEqual({ folder: "payments-id" });
  });

  it("offers New contract in a team with contract:create", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/folders/ledger-id/contracts", {
      folders,
      permissions: ["contract:create"],
    });
    expect(
      await screen.findByText("Contracts created in this team appear here."),
    ).toBeVisible();
    await user.click(
      await screen.findByRole("button", { name: "New contract" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/new");
    });
    expect(router.state.location.search).toEqual({ folder: "ledger-id" });
  });

  it("hides New contract in an archived team", async () => {
    renderApp("/folders/fraud-id/contracts", {
      folders: [...folders, fraud],
      permissions: ["contract:create"],
    });
    expect(
      await screen.findByRole(
        "heading",
        { name: "No contracts yet" },
        { timeout: 10_000 },
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "New contract" })).toBeNull();
  });

  it("shows a ProblemAlert when the list fails", async () => {
    renderApp("/folders/ledger-id/contracts", {
      folders,
      handlers: [
        http.get("*/api/v1/contracts/contracts", () =>
          problem(500, {
            type: "urn:dc:problem:internal",
            title: "Contracts unavailable",
          }),
        ),
      ],
    });
    expect(await screen.findByText("Contracts unavailable")).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry loading" })).toBeVisible();
  });
});

describe("folders page states", () => {
  it("shows a ProblemAlert when the folder list fails", async () => {
    renderApp("/folders", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/folders", () =>
          problem(500, {
            type: "urn:dc:problem:internal",
            title: "Folder list failed",
          }),
        ),
      ],
    });
    expect(await screen.findByText("Folder list failed")).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry loading" })).toBeVisible();
  });

  it("loads the folder list again on Retry loading", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/folders", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/folders", () => {
          calls += 1;
          return calls === 1
            ? problem(500, {
                type: "urn:dc:problem:internal",
                title: "Folder list failed",
              })
            : HttpResponse.json({ items: folders, next_cursor: null });
        }),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Retry loading" }),
    );
    expect(await screen.findByRole("treegrid")).toBeVisible();
    expect(calls).toBe(2);
  });

  it("shows archived folders on request and offers to create the first organization", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/folders", {
      folders: [rootFolder, { ...acme, archived: true }],
    });
    expect(
      await screen.findByText(
        "Create an organization above to start the hierarchy: organizations hold projects, projects hold teams, and contracts can live at any level.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("checkbox", { name: "Show archived" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ archived: true });
    });
    const tree = await screen.findByRole("treegrid", { name: "All folders" });
    const row = within(tree).getByRole("row", { name: "Acme, archived" });
    // Archived folders offer no create action, only Open.
    expect(
      within(row).queryByRole("button", { name: "Create project in Acme" }),
    ).toBeNull();
    expect(within(row).getByRole("link", { name: "Open Acme" })).toBeVisible();
    // The platform row reads "Platform" and teams show no subfolder count.
    expect(within(tree).getByRole("row", { name: "Root" })).toHaveTextContent(
      "Platform",
    );
  });

  it("opens the create organization dialog from the one header button of the empty page", async () => {
    const user = userEvent.setup();
    renderApp("/folders", { folders: [rootFolder] });
    const heading = await screen.findByRole("heading", {
      name: "No folders yet",
    });
    // Only the header offers it; the empty state adds no second button.
    await waitFor(() => {
      expect(
        screen.getAllByRole("button", { name: "Create organization" }),
      ).toHaveLength(1);
    });
    await user.click(
      screen.getByRole("button", { name: "Create organization" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Create organization",
    });
    expect(
      within(dialog).getByText("The new organization is created inside Root."),
    ).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(heading).toBeVisible();
  });

  it("counts a full page of contracts as 100+ and marks a team's subfolders as none", async () => {
    renderApp("/folders", {
      folders: [rootFolder, acme, payments, ledger],
      me: makeMe(),
      handlers: [
        http.get("*/api/v1/contracts/contracts", ({ request }) => {
          const id = new URL(request.url).searchParams.get("folder_id");
          return HttpResponse.json({
            items: id === "acme-id" ? [contractRow()] : [],
            next_cursor: id === "acme-id" ? "more" : null,
          });
        }),
      ],
    });
    const user = userEvent.setup();
    const tree = await screen.findByRole("treegrid", { name: "All folders" });
    const acmeRow = within(tree).getByRole("row", { name: "Acme" });
    await waitFor(() => {
      expect(within(acmeRow).getAllByRole("gridcell")[3]).toHaveTextContent(
        "100+",
      );
    });
    acmeRow.focus();
    await user.keyboard("{ArrowRight}{ArrowRight}{ArrowRight}");
    const ledgerRow = within(tree).getByRole("row", { name: "Ledger" });
    expect(within(ledgerRow).getAllByRole("gridcell")[2]).toHaveTextContent(
      "None",
    );
    // A team cannot hold folders: no create action on its row.
    expect(
      within(ledgerRow).queryByRole("button", { name: /Create/ }),
    ).toBeNull();
  });
});

describe("sorting and overview details", () => {
  it("sorts the Folders tab by name and by update time", async () => {
    const user = userEvent.setup();
    const older = makeFolder({
      id: "zeta-id",
      parent_id: "payments-id",
      kind: "team",
      display_name: "Zeta",
      path: "acme/payments/zeta",
      updated_at: "2026-10-01T10:00:00Z",
    });
    renderApp("/folders/payments-id/folders", {
      folders: [...folders, older],
    });
    const table = await screen.findByRole("table", {
      name: "Folders in Payments",
    });
    const names = () =>
      within(table)
        .getAllByRole("rowheader")
        .map((cell) => cell.textContent);
    expect(names()).toEqual(["Ledger", "Zeta"]);
    await user.click(within(table).getByRole("button", { name: "Name" }));
    await user.click(within(table).getByRole("button", { name: "Name" }));
    expect(names()).toEqual(["Zeta", "Ledger"]);
    await user.click(within(table).getByRole("button", { name: "Updated" }));
    expect(names()).toEqual(["Zeta", "Ledger"]);
  });

  it("sorts the Contracts tab by name, falling back to the ODCS id, and by update time", async () => {
    const user = userEvent.setup();
    renderApp("/folders/ledger-id/contracts", {
      folders,
      handlers: [
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({
            items: [
              contractRow({ id: "c-1", name: "Orders" }),
              contractRow({
                id: "c-2",
                name: null,
                odcs_id: "urn:alpha",
                updated_at: "2026-11-05T10:00:00Z",
              }),
            ],
            next_cursor: null,
          }),
        ),
      ],
    });
    const table = await screen.findByRole("table", {
      name: "Contracts in Ledger",
    });
    const names = () =>
      within(table)
        .getAllByRole("rowheader")
        .map((cell) => cell.textContent);
    expect(names()).toEqual(["Orders", "urn:alpha"]);
    await user.click(within(table).getByRole("button", { name: "Name" }));
    expect(names()).toEqual(["Orders", "urn:alpha"]);
    await user.click(within(table).getByRole("button", { name: "Name" }));
    expect(names()).toEqual(["urn:alpha", "Orders"]);
    await user.click(within(table).getByRole("button", { name: "Updated" }));
    expect(names()).toEqual(["Orders", "urn:alpha"]);
  });

  it("shows blocked inheritance, an unknown archive time and an unparseable date as given", async () => {
    const odd = makeFolder({
      id: "odd-id",
      display_name: "Odd",
      path: "odd",
      archived: true,
      archived_at: null,
      block_inheritance: true,
      updated_at: "not-a-date",
    });
    renderApp("/folders/odd-id", { folders: [rootFolder, odd] });
    expect(
      await screen.findByText(
        "Blocked: roles granted above this folder do not apply here",
      ),
    ).toBeVisible();
    const state = screen.getByRole("region", { name: "State" });
    expect(within(state).getByText("Not set")).toBeVisible();
    expect(within(state).getByText(/not-a-date/)).toBeVisible();
  });
});

function deletionCheck(
  blockers: { kind: string; count: number }[],
  purges: { kind: string; count: number }[] = [],
) {
  return http.get(
    "*/api/v1/identity/folders/:id/deletion-check",
    ({ params }) =>
      HttpResponse.json({
        folder_id: params.id,
        deletable: blockers.length === 0,
        blockers,
        purges,
      }),
  );
}

describe("delete folder", () => {
  it("deletes a deletable team through the dialog and lands on the parent", async () => {
    let sent: { method: string; path: string } | null = null;
    let release: () => void = () => undefined;
    const answered = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { router } = renderApp("/folders/ledger-id", {
      folders,
      handlers: [
        deletionCheck(
          [],
          [
            { kind: "deleted_contracts", count: 2 },
            { kind: "unused_rule_sets", count: 1 },
          ],
        ),
        http.delete("*/api/v1/identity/folders/:id", async ({ request }) => {
          sent = {
            method: request.method,
            path: new URL(request.url).pathname,
          };
          // Held until the busy label was seen (a fixed delay raced a loaded host).
          await answered;
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    const user = await openMenuItem("Ledger", "Delete folder…");
    const dialog = await screen.findByRole("alertdialog", {
      name: "Delete Ledger?",
    });
    expect(within(dialog).getByText(/acme\/payments\/ledger/)).toBeVisible();
    expect(
      within(dialog).getByText(
        /Also permanently removes 2 deleted contracts and 1 unused rule set\./,
      ),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Keep Ledger" }),
    ).toHaveFocus();
    await user.click(
      within(dialog).getByRole("button", { name: "Delete Ledger" }),
    );
    expect(
      await within(dialog).findByRole("button", { name: "Deleting…" }),
    ).toBeVisible();
    await waitFor(() => {
      expect(sent).toEqual({
        method: "DELETE",
        path: "/api/v1/identity/folders/ledger-id",
      });
    });
    release();
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders/payments-id");
    });
    expect(await screen.findByText("Deleted Ledger.")).toBeVisible();
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("goes to the Folders page when the parent is ROOT", async () => {
    const { router } = renderApp("/folders/acme-id", {
      folders,
      handlers: [
        http.delete(
          "*/api/v1/identity/folders/:id",
          () => new HttpResponse(null, { status: 204 }),
        ),
      ],
    });
    const user = await openMenuItem("Acme", "Delete folder…");
    const dialog = await screen.findByRole("alertdialog", {
      name: "Delete Acme?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Delete Acme" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders");
    });
  });

  it("is aria-disabled but focusable with the blockers as its reason, and sends no DELETE", async () => {
    let deletes = 0;
    renderApp("/folders/payments-id", {
      folders,
      handlers: [
        deletionCheck([
          { kind: "subfolders", count: 2 },
          { kind: "contracts", count: 1 },
          { kind: "rule_sets", count: 1 },
        ]),
        http.delete("*/api/v1/identity/folders/:id", () => {
          deletes += 1;
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole(
        "button",
        { name: "Actions for Payments" },
        { timeout: 10_000 },
      ),
    );
    const reason =
      "Delete is unavailable while this folder contains: 2 subfolders, 1 contract, 1 rule set. Move or delete them first.";
    const item = await screen.findByRole("menuitem", {
      name: new RegExp(`Delete folder…`),
    });
    await waitFor(() => {
      expect(item).toHaveAttribute("aria-disabled", "true");
      expect(item).toHaveAccessibleDescription(reason);
    });
    await user.click(item);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.getByRole("menu")).toBeVisible();
    expect(deletes).toBe(0);
  });

  it("names the archived folder above and a failed check", async () => {
    renderApp("/folders/ledger-id", {
      folders,
      handlers: [deletionCheck([{ kind: "archived_ancestor", count: 1 }])],
    });
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole(
        "button",
        { name: "Actions for Ledger" },
        { timeout: 10_000 },
      ),
    );
    expect(
      await screen.findByRole("menuitem", {
        name: /Delete folder…/,
        description:
          "Delete is unavailable while a folder above this one is archived. Unarchive it first.",
      }),
    ).toBeVisible();
  });

  it("is disabled with a reason when the deletion check fails", async () => {
    renderApp("/folders/ledger-id", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/folders/:id/deletion-check", () =>
          problem(503, {
            type: "urn:dc:problem:service-unavailable",
            title: "Service Unavailable",
          }),
        ),
      ],
    });
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole(
        "button",
        { name: "Actions for Ledger" },
        { timeout: 10_000 },
      ),
    );
    expect(
      await screen.findByRole("menuitem", {
        name: /Delete folder…/,
        description:
          "Delete is unavailable right now because the folder contents could not be checked.",
      }),
    ).toHaveAttribute("aria-disabled", "true");
  });

  it("keeps the dialog open with the problem on a 409 and refreshes the check", async () => {
    let checks = 0;
    renderApp("/folders/ledger-id", {
      folders,
      handlers: [
        http.get(
          "*/api/v1/identity/folders/:id/deletion-check",
          ({ params }) => {
            checks += 1;
            return HttpResponse.json({
              folder_id: params.id,
              deletable: checks === 1,
              blockers: checks === 1 ? [] : [{ kind: "contracts", count: 1 }],
              purges: [],
            });
          },
        ),
        http.delete("*/api/v1/identity/folders/:id", () =>
          problem(409, {
            type: "urn:dc:problem:folder-not-empty",
            title: "The folder is not empty",
            blockers: [{ kind: "contracts", count: 1 }],
          }),
        ),
      ],
    });
    const user = await openMenuItem("Ledger", "Delete folder…");
    const dialog = await screen.findByRole("alertdialog", {
      name: "Delete Ledger?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Delete Ledger" }),
    );
    expect(
      await within(dialog).findByText("The folder is not empty"),
    ).toBeVisible();
    await waitFor(() => {
      expect(checks).toBeGreaterThan(1);
    });
  });

  it("never offers Delete or asks the check on ROOT", async () => {
    let checks = 0;
    renderApp(`/folders/${ROOT_ID}`, {
      folders,
      handlers: [
        http.get("*/api/v1/identity/folders/:id/deletion-check", () => {
          checks += 1;
          return HttpResponse.json({});
        }),
      ],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Root" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Actions for Root" }),
    ).toBeNull();
    expect(checks).toBe(0);
  });

  it("never offers Delete or asks the check without folder:delete", async () => {
    let checks = 0;
    renderApp("/folders/ledger-id", {
      folders,
      permissions: ["folder:create", "folder:update"],
      handlers: [
        http.get("*/api/v1/identity/folders/:id/deletion-check", () => {
          checks += 1;
          return HttpResponse.json({});
        }),
      ],
    });
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole(
        "button",
        { name: "Actions for Ledger" },
        { timeout: 10_000 },
      ),
    );
    expect(
      screen.getAllByRole("menuitem").map((item) => item.textContent),
    ).toEqual(["Rename folder"]);
    expect(checks).toBe(0);
  });
});

describe("folder answers without a body", () => {
  const empty = () => new HttpResponse(null, { status: 204 });

  it("shows the generic problem in the rename dialog", async () => {
    renderApp("/folders/payments-id", {
      folders,
      handlers: [http.patch("*/api/v1/identity/folders/:id", empty)],
    });
    const user = await openMenuItem("Payments", "Rename folder");
    const dialog = await screen.findByRole("dialog", {
      name: "Rename Payments",
    });
    await user.type(within(dialog).getByLabelText("Name"), " 2");
    await user.click(
      within(dialog).getByRole("button", { name: "Rename folder" }),
    );
    expect(
      await within(dialog).findByText("Something went wrong"),
    ).toBeVisible();
  });

  it("shows the generic problem in the create dialog", async () => {
    const user = userEvent.setup();
    renderApp("/folders/payments-id", {
      folders,
      handlers: [http.post("*/api/v1/identity/folders", empty)],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create team" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create team" });
    await user.type(within(dialog).getByLabelText("Name"), "Risk");
    await user.click(
      within(dialog).getByRole("button", { name: "Create team" }),
    );
    expect(
      await within(dialog).findByText("Something went wrong"),
    ).toBeVisible();
  });

  it("shows the generic problem for the folder's contract list", async () => {
    renderApp("/folders/ledger-id/contracts", {
      folders,
      handlers: [http.get("*/api/v1/contracts/contracts", empty)],
    });
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });

  it("says Delete cannot be checked when the check comes back empty", async () => {
    const user = userEvent.setup();
    renderApp("/folders/ledger-id", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/folders/:id/deletion-check", empty),
      ],
    });
    await user.click(
      await screen.findByRole(
        "button",
        { name: "Actions for Ledger" },
        { timeout: 10_000 },
      ),
    );
    expect(
      await screen.findByRole("menuitem", {
        name: /Delete folder…/,
        description:
          "Delete is unavailable right now because the folder contents could not be checked.",
      }),
    ).toHaveAttribute("aria-disabled", "true");
  });
});
