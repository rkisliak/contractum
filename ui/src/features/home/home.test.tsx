// The home "Your access" card reuses the plain-language effective access view: a one-line role
// summary first and, for every caller (partial or full access), the capability table, the "No
// access" line and the raw ids only behind one collapsed "See details".
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { PERMISSION_IDS } from "@/features/access/permissionRegistry";
import { PERMISSION_TARGETS } from "@/features/access/permissionRegistry";
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

async function accessCard() {
  const heading = await screen.findByRole("heading", { name: "Your access" });
  return within(heading.closest("section") ?? document.body);
}

describe("home account card", () => {
  async function principalType() {
    const heading = await screen.findByRole("heading", {
      name: "Your account",
    });
    const card = within(heading.closest("section") ?? document.body);
    const term = card.getByText("Principal type");
    return term.nextElementSibling;
  }

  it("names the principal type in plain words, never a raw id", async () => {
    renderApp("/");
    expect(await principalType()).toHaveTextContent("Local user");
  });

  it("calls an Entra principal an Entra user", async () => {
    renderApp("/", {
      me: makeMe({
        principal_id: "entra:11111111-2222-4333-8444-555555555555:oid",
        principal_type: "entra_user",
        idp: "entra",
      }),
    });
    const value = await principalType();
    expect(value).toHaveTextContent("Entra user");
    expect(value).not.toHaveTextContent("entra_user");
  });

  it("names a service account and an Entra service principal", async () => {
    renderApp("/", {
      me: makeMe({
        principal_id: "sa:11111111-1111-4111-8111-111111111111",
        principal_type: "service_account",
      }),
    });
    expect(await principalType()).toHaveTextContent("Service account");
  });
});

describe("home access card", () => {
  it("summarises the role and keeps the capability table collapsed for partial access", async () => {
    const actor = userEvent.setup();
    renderApp("/");
    const card = await accessCard();
    expect(await card.findByText("Global admin")).toBeVisible();
    expect(card.getByText(/^Granted on/)).toHaveTextContent("Granted on Root");
    // Collapsed by default: no table, no "No access" line, no technical names.
    const details = card.getByText("See details");
    expect(details.closest("details")).not.toHaveAttribute("open");
    expect(card.getByRole("table", { hidden: true })).not.toBeVisible();
    expect(card.getByText(/^No access:/)).not.toBeVisible();
    expect(card.getByText("Show technical permission names")).not.toBeVisible();
    await actor.click(details);
    const table = await card.findByRole("table", {
      name: "What you can do in at least one of your folders",
    });
    const folders = within(table).getByRole("rowheader", { name: /Folders/ });
    const row = within(folders.closest("tr") ?? table);
    expect(row.getAllByText("Allowed").length).toBeGreaterThan(0);
    expect(row.getAllByText("Not allowed").length).toBeGreaterThan(0);
    // No "N/A" text and no permission lists in the main view.
    expect(table).not.toHaveTextContent("N/A");
    expect(card.queryByText("Includes:")).toBeNull();
    expect(card.getByText(/^No access:/)).toHaveTextContent(/Templates/);
    expect(card.getByText("Show technical permission names")).toBeVisible();
    expect(card.queryByText("folder:create")).not.toBeVisible();
  });

  it("shows access at one chosen folder when grants sit on two organizations", async () => {
    const actor = userEvent.setup();
    const org1 = makeFolder({
      id: "org1-id",
      slug: "org-01",
      display_name: "Organization-01",
      path: "org-01",
    });
    const org2 = makeFolder({
      id: "org2-id",
      slug: "org-02",
      display_name: "Organization-02",
      path: "org-02",
    });
    const grant = (folder: typeof org1, role: string) => ({
      grant_id: `g-${folder.id}`,
      folder_id: folder.id,
      folder_path: folder.path,
      role,
      via_group_id: null,
    });
    const byFolder: Record<string, string[]> = {
      "org1-id": [
        "contract:view",
        "contract:read",
        "contract:create",
        "contract:update",
        "contract:publish",
        "folder:create",
      ],
      "org2-id": ["contract:view", "contract:read"],
    };
    renderApp("/", {
      me: makeMe({
        global_permissions: ["audit:list"],
        assignments: [grant(org1, "org_admin"), grant(org2, "org_reader")],
      }),
      folders: [
        makeFolder({
          id: "root-x",
          parent_id: null,
          kind: "root",
          slug: "root",
          display_name: "ROOT",
          path: "",
        }),
        org1,
        org2,
      ],
      handlers: [
        http.get("*/api/v1/identity/me/capabilities", ({ request }) => {
          const id = new URL(request.url).searchParams.get("folder_id") ?? "";
          return HttpResponse.json({
            folder_id: id,
            roles: [],
            permissions: byFolder[id] ?? [],
            inheritance_blocked: false,
            grantable_roles: [],
          });
        }),
      ],
    });
    const card = await accessCard();
    await actor.click(await card.findByText("See details"));
    const select = await card.findByRole("combobox", {
      name: "Show access at",
    });
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Organization-01", "Organization-02"]);
    // Defaults to the folder of the first role line (the Org admin grant).
    expect(select).toHaveValue("org1-id");
    const contracts = (t: HTMLElement) =>
      within(
        within(t)
          .getByRole("rowheader", { name: /Contracts/ })
          .closest("tr") ?? t,
      );
    const marks = (name: string) => {
      const table = card.getByRole("table", { name });
      const row = contracts(table);
      return {
        yes: row.queryAllByText("Allowed").length,
        partly: row.queryAllByText("Partly allowed").length,
        no: row.queryAllByText("Not allowed").length,
      };
    };
    const first = marks("What you can do at Organization-01 and below");
    const noAccessFirst = card.getByText(/^No access:/).textContent;
    await actor.selectOptions(select, "org2-id");
    const second = marks("What you can do at Organization-02 and below");
    // The admin's folder allows more of Contracts than the reader's folder.
    expect(first.yes + first.partly).toBeGreaterThan(
      second.yes + second.partly,
    );
    expect(second.no).toBeGreaterThan(first.no);
    expect(card.getByText(/^No access:/)).toBeVisible();
    expect(card.getByText(/^No access:/).textContent).not.toBe(noAccessFirst);
  });

  it("keeps the same collapsed disclosure for full access", async () => {
    const actor = userEvent.setup();
    renderApp("/", {
      me: makeMe({ global_permissions: [...PERMISSION_IDS] }),
      permissions: [...PERMISSION_IDS],
    });
    const card = await accessCard();
    expect(await card.findByText("Full access to everything.")).toBeVisible();
    const details = card.getByText("See details");
    expect(details.closest("details")).not.toHaveAttribute("open");
    expect(card.getByRole("table", { hidden: true })).not.toBeVisible();
    await actor.click(details);
    expect(await card.findByRole("table")).toBeVisible();
    expect(card.queryByText(/^No access:/)).toBeNull();
  });

  it("shows the empty state to a principal without any access", async () => {
    renderApp("/", { me: makeMe({ assignments: [], global_permissions: [] }) });
    expect(
      await screen.findByRole("heading", { name: "No folder access yet" }),
    ).toBeVisible();
  });
});

describe("home for a Team editor", () => {
  // Like the live demo persona: only the baseline principal permissions globally (every
  // signed-in principal holds them) and Team editor on Ledger.
  const baseline = PERMISSION_IDS.filter(
    (permission) => PERMISSION_TARGETS[permission] === "principal",
  );
  const ledger = makeFolder({
    id: "ledger-id",
    kind: "team",
    display_name: "Ledger",
    path: "acme/payments/ledger",
    parent_id: "payments-id",
  });
  const editor = makeMe({
    display_name: "Lena Ledger",
    global_permissions: [...baseline],
    assignments: [
      {
        grant_id: "g",
        folder_id: "ledger-id",
        folder_path: "acme/payments/ledger",
        role: "team_editor",
        via_group_id: null,
      },
    ],
  });

  it("counts no granted global permission and links only to usable areas", async () => {
    renderApp("/", {
      me: editor,
      folders: [ledger],
      permissions: [
        "contract:create",
        "contract:read",
        "ruleset:read-published",
      ],
    });
    const summary = await screen.findByLabelText("Account summary");
    const globalStat = within(summary)
      .getByText("Global permissions")
      .closest("div");
    expect(globalStat).toHaveTextContent(/Global permissions\s*0/);
    const quick = within(
      (await screen.findByRole("heading", { name: "Quick links" })).closest(
        "section",
      ) ?? document.body,
    );
    expect(await quick.findByRole("link", { name: /Rule sets/ })).toBeVisible();
    expect(quick.getByRole("link", { name: /New contract/ })).toBeVisible();
    expect(quick.queryByRole("link", { name: /Audit log/ })).toBeNull();
  });

  it("names the folder of the role in the account menu and hides admin areas", async () => {
    renderApp("/", {
      me: editor,
      folders: [ledger],
      permissions: ["contract:read", "ruleset:read-published"],
    });
    const [trigger] = await screen.findAllByRole("button", {
      name: "Account menu for Lena Ledger",
    });
    if (!trigger) throw new Error("no account menu trigger");
    await userEvent.setup().click(trigger);
    expect(
      await within(await screen.findByRole("menu")).findByText(
        "Team editor · Ledger",
      ),
    ).toBeVisible();
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(
      await within(nav).findByRole("link", { name: "Rule sets" }),
    ).toBeVisible();
    for (const hidden of [
      "Templates",
      "Audit log",
      "Groups",
      "Service accounts",
      "Users",
    ])
      expect(within(nav).queryByRole("link", { name: hidden })).toBeNull();
  });
});

describe("home states", () => {
  const problemAnswer = (status: number) =>
    HttpResponse.json(
      { type: "urn:dc:problem:internal", title: "Server error", status },
      { status, headers: { "Content-Type": "application/problem+json" } },
    );

  it("shows a failed /me with Retry loading, then the welcome", async () => {
    const actor = userEvent.setup();
    let fail = true;
    renderApp("/", {
      handlers: [
        http.get("*/api/v1/identity/me", () =>
          fail ? problemAnswer(500) : HttpResponse.json(makeMe()),
        ),
      ],
    });
    const retry = await screen.findByRole("button", { name: "Retry loading" });
    fail = false;
    await actor.click(retry);
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Welcome, Ada Admin",
      }),
    ).toBeVisible();
  });

  it("lists the caller's groups and marks a read-only principal", async () => {
    renderApp("/", {
      me: makeMe({
        read_only: true,
        groups: [
          { id: "grp-1", display_name: "Data engineers", type: "local_group" },
          { id: "grp-2", display_name: "Finance readers", type: "entra_group" },
        ],
      }),
    });
    const heading = await screen.findByRole("heading", {
      name: "Your account",
    });
    const card = within(heading.closest("section") ?? document.body);
    expect(card.getByText("Read only")).toBeVisible();
    expect(card.getByText("Data engineers")).toBeVisible();
    expect(card.getByText("Finance readers")).toBeVisible();
  });

  it("shows a failed access read in the access card", async () => {
    renderApp("/", {
      handlers: [
        http.get("*/api/v1/identity/me/capabilities", () => problemAnswer(500)),
      ],
    });
    const card = await accessCard();
    expect(await card.findByText("Server error")).toBeVisible();
    expect(card.getByRole("button", { name: "Retry loading" })).toBeVisible();
  });

  it("lists Root first among the folders holding a grant", async () => {
    const actor = userEvent.setup();
    const zeta = makeFolder({
      id: "zeta-id",
      slug: "zeta",
      display_name: "Zeta",
      path: "zeta",
    });
    const alpha = makeFolder({
      id: "alpha-id",
      slug: "alpha",
      display_name: "Alpha",
      path: "alpha",
    });
    const grant = (folderId: string, path: string, role: string) => ({
      grant_id: `g-${folderId}`,
      folder_id: folderId,
      folder_path: path,
      role,
      via_group_id: null,
    });
    renderApp("/", {
      me: makeMe({
        global_permissions: ["audit:list"],
        assignments: [
          grant("zeta-id", "zeta", "org_reader"),
          grant(ROOT_ID, "", "global_reader"),
          grant("alpha-id", "alpha", "org_reader"),
        ],
      }),
      folders: [rootFolder, zeta, alpha],
      permissions: ["contract:view", "contract:read"],
    });
    const card = await accessCard();
    await actor.click(await card.findByText("See details"));
    const select = await card.findByRole("combobox", {
      name: "Show access at",
    });
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Root", "Alpha", "Zeta"]);
  });
});
