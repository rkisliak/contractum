// The signed-in shell through the real router: landmarks, the skip link, the registry-driven
// sidebar with the folder tree, global search, the account menu and sign out.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, makeMe, renderApp, rootFolder } from "@/test/renderApp";

afterEach(() => {
  tokenStore.clear();
  window.localStorage.clear();
  vi.restoreAllMocks();
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

describe("app shell", () => {
  it("renders the landmarks, skip link and home content", async () => {
    renderApp("/");
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Welcome, Ada Admin",
      }),
    ).toBeVisible();
    // Wide screens: the icon rail and the navigation panel; the compact top bar is for < 1024px.
    expect(
      screen.getByRole("navigation", { name: "Sections" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Primary" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
    expect(
      screen.getByRole("link", { name: "Skip to content" }),
    ).toHaveAttribute("href", "#main-content");
    await waitFor(() => {
      expect(document.title).toBe("Home · Contractum");
    });
  });

  it("lists the registry entries the principal may reach, with the current one marked", async () => {
    renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    const home = await within(nav).findByRole("link", { name: "Home" });
    expect(home).toHaveAttribute("aria-current", "page");
    expect(
      await within(nav).findByRole("link", { name: "Users" }),
    ).toBeVisible();
    expect(
      within(nav).getByRole("link", { name: "Folders" }),
    ).not.toHaveAttribute("aria-current");
  });

  it("hides admin entries from a principal without the permissions", async () => {
    renderApp("/", { me: makeMe({ global_permissions: [], assignments: [] }) });
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    await within(nav).findByRole("link", { name: "Folders" });
    expect(within(nav).queryByRole("link", { name: "Users" })).toBeNull();
    expect(within(nav).queryByRole("link", { name: "Contracts" })).toBeNull();
  });

  it("keeps the folder tree out of the navigation panel", async () => {
    renderApp("/", { folders: [rootFolder, acme, payments] });
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(within(nav).getByRole("link", { name: "Folders" })).toBeVisible();
    expect(screen.queryByRole("treegrid")).toBeNull();
  });

  it("opens a folder from the /folders navigator with the keyboard", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/folders", {
      folders: [rootFolder, acme, payments],
    });
    const tree = await screen.findByRole("treegrid", { name: "All folders" });
    const acmeItem = within(tree).getByRole("row", { name: "Acme" });
    expect(acmeItem).toHaveAttribute("aria-expanded", "false");
    acmeItem.focus();
    await user.keyboard("{ArrowRight}");
    expect(acmeItem).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{ArrowDown}");
    const paymentsItem = within(tree).getByRole("row", {
      name: "Payments",
    });
    expect(paymentsItem).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders/payments-id");
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Payments" }),
    ).toBeVisible();
  });

  it("sends the global search to the contract list with q", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/");
    await screen.findByRole("heading", { level: 1, name: /Welcome/ });
    await user.type(
      screen.getByRole("searchbox", { name: "Search contracts" }),
      "orders{Enter}",
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts");
    });
    expect(router.state.location.search).toEqual({ q: "orders" });
  });

  it("opens the account menu and signs out through the session", async () => {
    const user = userEvent.setup();
    let loggedOut = false;
    const { session } = renderApp("/", {
      handlers: [
        http.post("*/api/v1/identity/auth/logout", () => {
          loggedOut = true;
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    await screen.findByRole("heading", { level: 1, name: /Welcome/ });
    // The account menu is the rail's avatar at 1024px and up and the top bar's below (only one
    // of them is visible at a time); the navigation panel holds none.
    const rail = screen.getByRole("navigation", {
      name: "Sections",
      hidden: true,
    });
    expect(
      within(rail).getByRole("button", {
        name: "Account menu for Ada Admin",
        hidden: true,
      }),
    ).toBeInTheDocument();
    expect(
      within(
        screen.getByRole("navigation", { name: "Primary" }).parentElement ??
          document.body,
      ).queryByRole("button", { name: /account menu/i }),
    ).toBeNull();
    const [trigger] = await screen.findAllByRole("button", {
      name: "Account menu for Ada Admin",
    });
    if (!trigger) throw new Error("no account menu trigger");
    await user.click(trigger);
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByText("Local account")).toBeVisible();
    expect(within(menu).getByText("Global admin · Root")).toBeVisible();
    expect(
      within(menu).getByRole("menuitem", { name: "Account settings" }),
    ).toHaveAttribute("href", "/account/profile");
    await user.click(within(menu).getByRole("menuitem", { name: "Sign out" }));
    await waitFor(() => {
      expect(loggedOut).toBe(true);
    });
    expect(tokenStore.get()).toBeNull();
    session.dispose();
  });

  it("lists each component's build in Versions, a silent service as Unavailable", async () => {
    const user = userEvent.setup();
    const ok = (service: string, version = "dev") =>
      http.get(`*/api/v1/${service}/version`, () =>
        HttpResponse.json({
          service,
          version,
          commit: "abc1234def56",
          built_at: "2026-11-15T20:00:00Z",
        }),
      );
    renderApp("/", {
      handlers: [
        ok("identity"),
        ok("validation"),
        ok("audit", "1.4.0"),
        ok("docs"),
        http.get("*/api/v1/contracts/version", () =>
          HttpResponse.json({}, { status: 503 }),
        ),
      ],
    });
    await screen.findByRole("heading", { level: 1, name: /Welcome/ });
    const [trigger] = await screen.findAllByRole("button", {
      name: "Account menu for Ada Admin",
    });
    if (!trigger) throw new Error("no account menu trigger");
    await user.click(trigger);
    await user.click(await screen.findByRole("menuitem", { name: "Versions" }));
    const dialog = await screen.findByRole("dialog", { name: "Versions" });
    const table = within(dialog).getByRole("table");
    const row = (name: string) =>
      within(
        within(table)
          .getByRole("rowheader", { name })
          .closest("tr") as HTMLElement,
      );
    // The UI build of a test reads "dev": the platform version leads the dialog.
    expect(within(dialog).getByText("Contractum dev")).toBeVisible();
    expect(row("Web UI").getAllByText("dev").length).toBeGreaterThan(0);
    expect(await row("Identity").findByText("abc1234def56")).toBeVisible();
    expect(await row("API docs").findByText("abc1234def56")).toBeVisible();
    expect(await row("Contracts").findByText("Unavailable")).toBeVisible();
    // A component on another version than the UI is named in text, not by colour alone.
    expect(await row("Audit").findByText("1.4.0")).toBeVisible();
    expect(row("Audit").getByText("Different version")).toBeVisible();
    expect(within(dialog).getAllByText("Different version")).toHaveLength(1);
    expect(within(dialog).queryByText("Request ID")).toBeNull();
  });
});

describe("the Versions dialog and the configured platform version", () => {
  it("leads with the version the UI was started with and marks another one", async () => {
    const user = userEvent.setup();
    const ok = (service: string, version: string) =>
      http.get(`*/api/v1/${service}/version`, () =>
        HttpResponse.json({
          service,
          version,
          commit: "abc1234def56",
          built_at: "2026-11-15T20:00:00Z",
        }),
      );
    renderApp("/", {
      config: { version: "2.0.0" },
      handlers: [
        ok("identity", "2.0.0"),
        ok("contracts", "2.0.0"),
        ok("validation", "2.0.0"),
        ok("audit", "1.4.0"),
        ok("docs", "2.0.0"),
      ],
    });
    await screen.findByRole("heading", { level: 1, name: /Welcome/ });
    await user.click(
      screen.getByRole("button", { name: "Versions", hidden: true }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Versions" });
    expect(within(dialog).getByText("Contractum 2.0.0")).toBeVisible();
    const row = (name: string) =>
      within(
        within(dialog)
          .getByRole("rowheader", { name })
          .closest("tr") as HTMLElement,
      );
    expect(row("Web UI").getByText("2.0.0")).toBeVisible();
    expect(await row("Identity").findByText("2.0.0")).toBeVisible();
    expect(await row("Audit").findByText("1.4.0")).toBeVisible();
    expect(row("Audit").getByText("Different version")).toBeVisible();
    expect(within(dialog).getAllByText("Different version")).toHaveLength(1);
  });
});

describe("app shell panels and failures", () => {
  async function home(options: Parameters<typeof renderApp>[1] = {}) {
    const user = userEvent.setup();
    const rendered = renderApp("/", options);
    await screen.findByRole("heading", { level: 1, name: /Welcome/ });
    return { user, ...rendered };
  }

  it("hides and shows the navigation panel from the rail and remembers it", async () => {
    const { user } = await home();
    await user.click(
      screen.getByRole("button", { name: "Collapse navigation", hidden: true }),
    );
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
    expect(window.localStorage.getItem("dc-sidebar-collapsed")).toBe("1");
    await user.click(
      screen.getByRole("button", { name: "Expand navigation", hidden: true }),
    );
    expect(
      screen.getByRole("navigation", { name: "Primary" }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("dc-sidebar-collapsed")).toBe("0");
  });

  it("starts collapsed when the browser remembered it", async () => {
    window.localStorage.setItem("dc-sidebar-collapsed", "1");
    await home();
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Expand navigation", hidden: true }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("still toggles the panel when local storage is blocked", async () => {
    const blocked = () => {
      throw new DOMException("blocked", "SecurityError");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(blocked);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(blocked);
    const { user } = await home();
    expect(
      screen.getByRole("navigation", { name: "Primary" }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Collapse navigation", hidden: true }),
    );
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
  });

  it("opens the navigation drawer from the top bar and closes it on a pick", async () => {
    const { user, router } = await home();
    await user.click(
      screen.getByRole("button", { name: "Open navigation", hidden: true }),
    );
    const drawer = await screen.findByRole("dialog", { name: "Navigation" });
    await user.click(within(drawer).getByRole("link", { name: "Folders" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders");
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Navigation" })).toBeNull();
    });
  });

  it("sends an empty global search to the unfiltered contract list", async () => {
    const { user, router } = await home();
    await user.type(
      screen.getByRole("searchbox", { name: "Search contracts" }),
      "   {Enter}",
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts");
    });
    expect(router.state.location.search).toEqual({});
  });

  it("says a failed sign-out did not complete, from either account menu", async () => {
    const { user } = await home({
      handlers: [
        http.post("*/api/v1/identity/auth/logout", () =>
          HttpResponse.json(
            { type: "urn:dc:problem:unavailable", title: "Down", status: 503 },
            {
              status: 503,
              headers: { "Content-Type": "application/problem+json" },
            },
          ),
        ),
      ],
    });
    const triggers = screen.getAllByRole("button", {
      name: "Account menu for Ada Admin",
      hidden: true,
    });
    expect(triggers).toHaveLength(2);
    for (const trigger of triggers) {
      await user.click(trigger);
      await user.click(
        await screen.findByRole("menuitem", { name: "Sign out" }),
      );
      expect(
        await screen.findByText("Sign-out did not complete"),
      ).toBeVisible();
      await user.click(screen.getByRole("button", { name: "Dismiss alert" }));
      expect(screen.queryByText("Sign-out did not complete")).toBeNull();
    }
    expect(tokenStore.get()).not.toBeNull();
  });

  it("names the folder of the first role by its path when the folder is not listed", async () => {
    const { user } = await home({
      me: makeMe({
        assignments: [
          {
            grant_id: "g-9",
            folder_id: "ledger-id",
            folder_path: "acme/payments/ledger",
            role: "team_reader",
            via_group_id: null,
          },
        ],
      }),
    });
    const [trigger] = screen.getAllByRole("button", {
      name: "Account menu for Ada Admin",
      hidden: true,
    });
    if (!trigger) throw new Error("no account menu trigger");
    await user.click(trigger);
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByText(/· ledger$/)).toBeVisible();
  });
});

describe("the Versions dialog", () => {
  it("shows a raw build time it cannot read, an empty one, an answer without a body, and closes", async () => {
    const user = userEvent.setup();
    renderApp("/", {
      handlers: [
        http.get("*/api/v1/identity/version", () =>
          HttpResponse.json({
            service: "identity",
            version: "0.2.0",
            commit: "",
            built_at: "not-a-date",
          }),
        ),
        http.get("*/api/v1/contracts/version", () =>
          HttpResponse.json({
            service: "contracts",
            version: "0.2.0",
            commit: "def5678",
          }),
        ),
        http.get(
          "*/api/v1/validation/version",
          () => new HttpResponse(null, { status: 204 }),
        ),
        http.get("*/api/v1/audit/version", () =>
          HttpResponse.json({
            service: "audit",
            version: "0.2.0",
            commit: "abc",
            built_at: "dev",
          }),
        ),
        http.get("*/api/v1/docs/version", () =>
          HttpResponse.json({}, { status: 404 }),
        ),
      ],
    });
    await screen.findByRole("heading", { level: 1, name: /Welcome/ });
    await user.click(
      screen.getByRole("button", { name: "Versions", hidden: true }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Versions" });
    const row = (name: string) =>
      within(
        within(dialog)
          .getByRole("rowheader", { name })
          .closest("tr") as HTMLElement,
      );
    expect(await row("Identity").findByText("not-a-date")).toBeVisible();
    expect(await row("Contracts").findByText("def5678")).toBeVisible();
    expect(await row("Validation").findByText("Unavailable")).toBeVisible();
    expect(await row("API docs").findByText("Unavailable")).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Versions" })).toBeNull();
    });
  });
});
