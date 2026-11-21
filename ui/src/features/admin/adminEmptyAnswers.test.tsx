// A 2xx answer without a body is a broken server, never an empty record or list: every admin
// screen that loads a record, a list or a related table shows the generic problem instead.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, makeMe, renderApp, rootFolder } from "@/test/renderApp";

vi.setConfig({ testTimeout: 60_000 });

afterEach(() => {
  tokenStore.clear();
});

const I = "*/api/v1/identity";
const empty = () => new HttpResponse(null, { status: 204 });
const acme = makeFolder({ id: "acme-id", slug: "acme", path: "acme" });
const admin = makeMe({
  global_permissions: [
    ...makeMe().global_permissions,
    "user:read",
    "pat:read-all",
    "entra:read",
  ],
});
const audit = {
  created_by: "local:admin",
  created_at: "2026-11-01T10:00:00Z",
  updated_by: "local:admin",
  updated_at: "2026-11-01T10:00:00Z",
};

describe("admin screens given an empty answer", () => {
  it.each([
    ["the users list", "/admin/users", `${I}/users`],
    ["the groups list", "/admin/groups", `${I}/groups`],
    [
      "the service accounts list",
      "/admin/service-accounts",
      `${I}/service-accounts`,
    ],
    ["a user", "/admin/users/u-1", `${I}/users/u-1`],
    ["a group", "/admin/groups/g-1", `${I}/groups/g-1`],
    [
      "a service account",
      "/admin/service-accounts/sa-1",
      `${I}/service-accounts/sa-1`,
    ],
    ["an Entra user", "/admin/entra/users/e-1", `${I}/entra/users/e-1`],
    ["my tokens", "/account/tokens", `${I}/pats`],
    ["every token", "/admin/pats", `${I}/admin/pats`],
    [
      "the folder grants",
      "/folders/acme-id/access",
      `${I}/folders/acme-id/grants`,
    ],
    [
      "the effective access",
      "/folders/acme-id/access",
      `${I}/folders/acme-id/effective-access`,
    ],
  ])("shows the generic problem for %s", async (_name, path, url) => {
    renderApp(path, {
      me: admin,
      folders: [rootFolder, acme],
      permissions: ["grant:read", "grant:manage", "folder:set-block"],
      handlers: [
        http.get(url, empty),
        http.get(`${I}/folders/acme-id/grants`, () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
        http.get(`${I}/folders/acme-id/effective-access`, () =>
          HttpResponse.json({
            folder_id: "acme-id",
            roles: [],
            permissions: [],
            inheritance_blocked: false,
            inheritance_blocked_at: null,
          }),
        ),
      ],
    });
    expect(
      await screen.findByText("Something went wrong", {}, { timeout: 20_000 }),
    ).toBeVisible();
  });

  it.each([
    ["the Entra users", "/admin/users", `${I}/entra/users`],
    ["the Entra groups", "/admin/groups", `${I}/entra/groups`],
    [
      "the Entra service principals",
      "/admin/service-accounts",
      `${I}/entra/service-principals`,
    ],
  ])(
    "names %s as not loaded, even with no local record yet",
    async (name, path, url) => {
      renderApp(path, {
        me: admin,
        folders: [rootFolder, acme],
        config: { entraEnabled: true },
        handlers: [http.get(url, empty)],
      });
      const label = name.replace(/^the E/, "E");
      expect(
        await screen.findByText(
          `${label} could not be loaded`,
          {},
          { timeout: 20_000 },
        ),
      ).toBeVisible();
      expect(screen.getByText("Something went wrong")).toBeVisible();
    },
  );

  it.each([
    [
      "a group's members",
      "/admin/groups/g-1",
      `${I}/groups/g-1`,
      {
        id: "g-1",
        name: "ledger-editors",
        owner_folder_id: rootFolder.id,
        ...audit,
      },
      `${I}/groups/g-1/members`,
    ],
    [
      "a service account's keys",
      "/admin/service-accounts/sa-1",
      `${I}/service-accounts/sa-1`,
      {
        id: "sa-1",
        principal_id: "sa:sa-1",
        name: "pipeline",
        owner_folder_id: rootFolder.id,
        is_active: true,
        ...audit,
      },
      `${I}/service-accounts/sa-1/keys`,
    ],
  ])(
    "shows the generic problem for %s",
    async (_name, path, recordUrl, record, url) => {
      renderApp(path, {
        me: admin,
        folders: [rootFolder, acme],
        handlers: [
          http.get(recordUrl, () => HttpResponse.json(record)),
          http.get(url, empty),
        ],
      });
      expect(
        await screen.findByText(
          "Something went wrong",
          {},
          { timeout: 20_000 },
        ),
      ).toBeVisible();
    },
  );

  it.each([
    ["a local group", "/admin/groups", `${I}/groups`, "Create local group"],
    [
      "a service account",
      "/admin/service-accounts",
      `${I}/service-accounts`,
      "Create service account",
    ],
  ])(
    "keeps the create dialog open with the generic problem for %s",
    async (_name, path, url, entry) => {
      const actor = userEvent.setup();
      renderApp(path, {
        me: admin,
        folders: [rootFolder, acme],
        handlers: [http.post(url, empty)],
      });
      await actor.click(
        await screen.findByRole("button", { name: "New" }, { timeout: 20_000 }),
      );
      await actor.click(await screen.findByRole("menuitem", { name: entry }));
      const dialog = within(await screen.findByRole("dialog", { name: entry }));
      await actor.type(dialog.getByLabelText("Name"), "nightly");
      await actor.click(dialog.getByRole("button", { name: entry }));
      expect(await dialog.findByText("Something went wrong")).toBeVisible();
    },
  );

  it("keeps the token dialog open with the generic problem", async () => {
    const actor = userEvent.setup();
    renderApp("/account/tokens", {
      me: admin,
      folders: [rootFolder, acme],
      handlers: [
        http.post(`${I}/pats`, empty),
        http.get(`${I}/pats`, () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
      ],
    });
    await actor.click(
      await screen.findByRole(
        "button",
        { name: "Create token" },
        { timeout: 20_000 },
      ),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Create token" }),
    );
    await actor.type(dialog.getByLabelText("Name"), "nightly");
    await actor.click(dialog.getByRole("button", { name: "Create token" }));
    expect(await dialog.findByText("Something went wrong")).toBeVisible();
  });
});
