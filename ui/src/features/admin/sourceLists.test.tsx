// The merged admin lists (quick 261119-l30): Users, Groups and Service accounts each show local and
// Microsoft Entra records in one table with a Source column, a bookmarkable All | Local | Entra
// filter (`?source=`), a search box (`?q=`) and one gated "New" menu; /admin/entra only redirects.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { delay } from "msw";
import type {
  EntraGroup,
  EntraServicePrincipal,
  EntraUser,
  LocalGroup,
  LocalUser,
  Me,
  ServiceAccount,
} from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { sortRowsBy } from "@/test/governance-table";
import { ENTRA_UNREACHABLE } from "./EntraRegisterDialog";
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

const LOAD = { timeout: 10_000 };
const TID = "11111111-2222-4333-8444-555555555555";
const OID = "0b5c3f8e-1a2b-4c3d-8e9f-0123456789ab";
const AUDIT = {
  created_by: "local:admin",
  created_at: "2026-11-01T10:00:00Z",
  updated_by: "local:admin",
  updated_at: "2026-11-01T10:00:00Z",
};
const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const folders = [rootFolder, acme];
const ENTRA = { config: { entraEnabled: true } };

const page = (items: unknown[], next: string | null = null) =>
  HttpResponse.json({ items, next_cursor: next });

function localUser(overrides: Partial<LocalUser> = {}): LocalUser {
  return {
    id: "u-1",
    principal_id: "local:u-1",
    username: "demo-alice",
    display_name: "Alice Demo",
    email: null,
    is_active: true,
    must_rotate: false,
    ...AUDIT,
    ...overrides,
  };
}

function entraUser(overrides: Partial<EntraUser> = {}): EntraUser {
  return {
    id: "eu-1",
    principal_id: `entra:${TID}:${OID}`,
    tenant_id: TID,
    object_id: OID,
    display_name: "Erin Entra",
    preferred_username: "erin@example.com",
    is_active: true,
    registered_directly: true,
    ...AUDIT,
    ...overrides,
  };
}

function localGroup(overrides: Partial<LocalGroup> = {}): LocalGroup {
  return {
    id: "grp-1",
    name: "demo-editors",
    owner_folder_id: "acme-id",
    ...AUDIT,
    ...overrides,
  };
}

function entraGroup(overrides: Partial<EntraGroup> = {}): EntraGroup {
  return {
    id: "eg-1",
    tenant_id: TID,
    object_id: OID,
    display_name: "Data Engineers",
    owner_folder_id: ROOT_ID,
    ...AUDIT,
    ...overrides,
  };
}

function serviceAccount(
  overrides: Partial<ServiceAccount> = {},
): ServiceAccount {
  return {
    id: "sa-1",
    principal_id: "sa:sa-1",
    name: "demo-pipeline",
    owner_folder_id: "acme-id",
    is_active: true,
    ...AUDIT,
    ...overrides,
  };
}

function entraSp(
  overrides: Partial<EntraServicePrincipal> = {},
): EntraServicePrincipal {
  return {
    id: "sp-1",
    principal_id: `entra:${TID}:${OID}`,
    tenant_id: TID,
    object_id: OID,
    display_name: "Ingest App",
    owner_folder_id: "acme-id",
    is_active: true,
    directly_registered: true,
    ...AUDIT,
    ...overrides,
  };
}

/** A Global admin who also holds the Entra permissions (the default test admin does not list them). */
const entraAdmin: Me = makeMe({
  global_permissions: [
    "user:read",
    "user:create",
    "entra:read",
    "entra:manage",
    "audit:list",
    "group:list",
  ],
});

const creator: Me = makeMe({
  global_permissions: ["user:read", "user:create", "audit:list", "group:list"],
});

/** The register dialogs open in search mode; these tests paste an object ID instead. */
async function useObjectId(
  actor: ReturnType<typeof userEvent.setup>,
  dialog: HTMLElement,
) {
  await actor.click(
    within(dialog).getByRole("button", { name: "Enter an object ID instead" }),
  );
}

describe("users", () => {
  it("merges local and Entra users by name with a Source column and the detail links", async () => {
    renderApp("/admin/users", {
      folders,
      ...ENTRA,
      handlers: [
        http.get("*/api/v1/identity/users", () =>
          page([
            localUser({ email: "alice@example.com" }),
            localUser({
              id: "u-2",
              username: "demo-zed",
              display_name: "Zed Demo",
              is_active: false,
            }),
          ]),
        ),
        http.get("*/api/v1/identity/entra/users", () => page([entraUser()])),
      ],
    });
    const table = await screen.findByRole("table", { name: "Users" }, LOAD);
    await within(table).findByRole("link", { name: "Erin Entra" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent.trim()),
    ).toEqual(["Name", "Username/email", "Source", "Status"]);
    // One table, sorted by name.
    expect(
      within(table)
        .getAllByRole("rowheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Alice Demo", "Erin Entra", "Zed Demo"]);
    const alice = within(table)
      .getByRole("link", { name: "Alice Demo" })
      .closest("tr");
    expect(alice).toHaveTextContent("demo-alice");
    expect(alice).toHaveTextContent("Local");
    expect(
      within(table).getByRole("link", { name: "Alice Demo" }),
    ).toHaveAttribute("href", "/admin/users/u-1");
    const erin = within(table)
      .getByRole("link", { name: "Erin Entra" })
      .closest("tr");
    expect(erin).toHaveTextContent("erin@example.com");
    expect(erin).toHaveTextContent("Entra");
    expect(
      within(table).getByRole("link", { name: "Erin Entra" }),
    ).toHaveAttribute("href", "/admin/entra/users/eu-1");
    expect(
      within(table).getByRole("link", { name: "Zed Demo" }).closest("tr"),
    ).toHaveTextContent("Deactivated");
    const actor = userEvent.setup();
    expect(await sortRowsBy(actor, table, "Username/email")).toEqual([
      "Alice Demo",
      "Zed Demo",
      "Erin Entra",
    ]);
    expect(await sortRowsBy(actor, table, "Source")).toEqual([
      "Erin Entra",
      "Alice Demo",
      "Zed Demo",
    ]);
    expect(await sortRowsBy(actor, table, "Name")).toEqual([
      "Alice Demo",
      "Erin Entra",
      "Zed Demo",
    ]);
    expect(await sortRowsBy(actor, table, "Name")).toEqual([
      "Zed Demo",
      "Erin Entra",
      "Alice Demo",
    ]);
  });

  it("keeps the Source filter and the search in the URL and requests only what is shown", async () => {
    const actor = userEvent.setup();
    const requested: string[] = [];
    const { router } = renderApp("/admin/users?source=entra", {
      folders,
      ...ENTRA,
      handlers: [
        http.get("*/api/v1/identity/users", () => {
          requested.push("local");
          return page([localUser()]);
        }),
        http.get("*/api/v1/identity/entra/users", () => {
          requested.push("entra");
          return page([
            entraUser(),
            entraUser({
              id: "eu-2",
              display_name: "Finn Former",
              preferred_username: null,
              object_id: "9a8b7c6d-1a2b-4c3d-8e9f-0123456789ab",
            }),
          ]);
        }),
      ],
    });
    expect(
      await screen.findByRole("link", { name: "Erin Entra" }, LOAD),
    ).toBeVisible();
    expect(screen.queryByRole("link", { name: "Alice Demo" })).toBeNull();
    expect(requested).toEqual(["entra"]);
    const source = screen.getByRole("group", { name: "Source" });
    expect(
      within(source).getByRole("button", { name: "Entra" }),
    ).toHaveAttribute("aria-pressed", "true");

    // Search matches the name, the username or email and the object ID.
    const search = screen.getByLabelText("Search users");
    await actor.type(search, "9a8b7c6d");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        source: "entra",
        q: "9a8b7c6d",
      });
    });
    expect(screen.getByRole("link", { name: "Finn Former" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Erin Entra" })).toBeNull();
    await actor.clear(search);
    await actor.type(search, "erin@");
    expect(screen.getByRole("link", { name: "Erin Entra" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Finn Former" })).toBeNull();
    await actor.clear(search);

    // All removes the param and brings the local users in.
    await actor.click(within(source).getByRole("button", { name: "All" }));
    expect(
      await screen.findByRole("link", { name: "Alice Demo" }),
    ).toBeVisible();
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    await actor.click(within(source).getByRole("button", { name: "Local" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ source: "local" });
    });
    expect(screen.queryByRole("link", { name: "Erin Entra" })).toBeNull();
  });

  it("restores a bookmarked search and says when nothing matches", async () => {
    renderApp("/admin/users?q=zzz", {
      folders,
      ...ENTRA,
      handlers: [
        http.get("*/api/v1/identity/users", () => page([localUser()])),
        http.get("*/api/v1/identity/entra/users", () => page([entraUser()])),
      ],
    });
    expect(
      await screen.findByText("No users match the search.", {}, LOAD),
    ).toBeVisible();
    expect(screen.getByLabelText("Search users")).toHaveValue("zzz");
  });

  it("never asks for Entra users and shows no Entra controls when Entra is not configured", async () => {
    const actor = userEvent.setup();
    let entraRequested = false;
    renderApp("/admin/users?source=entra", {
      folders,
      me: creator,
      handlers: [
        http.get("*/api/v1/identity/users", () => page([localUser()])),
        http.get("*/api/v1/identity/entra/users", () => {
          entraRequested = true;
          return page([entraUser()]);
        }),
      ],
    });
    // A source=entra URL is treated as All when Entra is not available.
    const table = await screen.findByRole("table", { name: "Users" }, LOAD);
    expect(
      within(table).getByRole("link", { name: "Alice Demo" }).closest("tr"),
    ).toHaveTextContent("Local");
    expect(screen.queryByRole("group", { name: "Source" })).toBeNull();
    await actor.click(screen.getByRole("button", { name: "New" }));
    expect(
      await screen.findByRole("menuitem", { name: "Create local user" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("menuitem", { name: "Register Entra user" }),
    ).toBeNull();
    expect(entraRequested).toBe(false);
  });

  it("asks for nothing without user:read and offers no New menu without user:create", async () => {
    const requested: string[] = [];
    renderApp("/admin/users", {
      folders,
      ...ENTRA,
      me: makeMe({ global_permissions: ["audit:list"] }),
      handlers: [
        http.get("*/api/v1/identity/users", () => {
          requested.push("local");
          return page([]);
        }),
        http.get("*/api/v1/identity/entra/users", () => {
          requested.push("entra");
          return page([]);
        }),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { name: "Not available at your access level" },
        LOAD,
      ),
    ).toBeVisible();
    expect(requested).toEqual([]);
    expect(screen.queryByRole("button", { name: "New" })).toBeNull();
    expect(screen.queryByRole("group", { name: "Source" })).toBeNull();
  });

  it("opens the create and register dialogs from the New menu", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/users", {
      folders,
      ...ENTRA,
      me: creator,
      handlers: [
        http.get("*/api/v1/identity/users", () => page([localUser()])),
        http.get("*/api/v1/identity/entra/users", () => page([entraUser()])),
      ],
    });
    await screen.findByRole("link", { name: "Erin Entra" }, LOAD);
    await actor.click(screen.getByRole("button", { name: "New" }));
    expect(
      screen.getAllByRole("menuitem").map((item) => item.textContent),
    ).toEqual(["Create local user", "Register Entra user"]);
    await actor.click(
      screen.getByRole("menuitem", { name: "Create local user" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Create local user" }),
    ).toBeVisible();
    await actor.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await actor.click(screen.getByRole("button", { name: "New" }));
    await actor.click(
      screen.getByRole("menuitem", { name: "Register Entra user" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Register Entra user" }),
    ).toBeVisible();
  });

  it("validates GUIDs, registers a user with a normalised body and refreshes the list", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    let registered = false;
    renderApp("/admin/users?source=entra", {
      folders,
      ...ENTRA,
      me: creator,
      handlers: [
        http.get("*/api/v1/identity/entra/users", () =>
          page(registered ? [entraUser()] : []),
        ),
        http.post("*/api/v1/identity/entra/users", async ({ request }) => {
          bodies.push(await request.json());
          registered = true;
          return HttpResponse.json(entraUser(), { status: 201 });
        }),
      ],
    });
    expect(
      await screen.findByText("No Entra users yet.", {}, LOAD),
    ).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "New" }));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Register Entra user" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Register Entra user",
    });
    expect(within(dialog).queryByLabelText("Owner")).toBeNull();
    await useObjectId(actor, dialog);
    await actor.type(within(dialog).getByLabelText("Object ID"), "nope");
    await actor.type(
      within(dialog).getByLabelText("Tenant ID (optional)"),
      "also-nope",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Register Entra user" }),
    );
    expect(
      within(dialog).getByText("Enter the object ID as a GUID."),
    ).toBeVisible();
    expect(
      within(dialog).getByText(
        "Enter the tenant ID as a GUID, or leave it empty.",
      ),
    ).toBeVisible();
    expect(bodies).toEqual([]);

    await actor.clear(within(dialog).getByLabelText("Object ID"));
    await actor.type(
      within(dialog).getByLabelText("Object ID"),
      `  ${OID.toUpperCase()} `,
    );
    await actor.clear(within(dialog).getByLabelText("Tenant ID (optional)"));
    await actor.click(
      within(dialog).getByRole("button", { name: "Register Entra user" }),
    );
    await waitFor(() => {
      expect(bodies).toEqual([{ object_id: OID, tenant_id: null }]);
    });
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Register Entra user" }),
      ).toBeNull();
    });
    expect(
      await screen.findByRole("link", { name: "Erin Entra" }),
    ).toBeVisible();
  });

  it("maps a 409 to the field, a 503 to the directory copy and another refusal to a ProblemAlert", async () => {
    const actor = userEvent.setup();
    const refuse = (status: number, slug: string, detail: string) =>
      HttpResponse.json(
        { type: `urn:dc:problem:${slug}`, title: "Refused", status, detail },
        { status, headers: { "Content-Type": "application/problem+json" } },
      );
    const replies = [
      () => refuse(409, "conflict", "Already there."),
      () => refuse(503, "entra-unavailable", "Graph down."),
      () => refuse(422, "entra-object-not-found", "No such object."),
    ];
    renderApp("/admin/users?source=entra", {
      folders,
      ...ENTRA,
      me: creator,
      handlers: [
        http.get("*/api/v1/identity/entra/users", () => page([])),
        http.post("*/api/v1/identity/entra/users", () => {
          const reply = replies.shift();
          if (!reply) throw new Error("unexpected POST");
          return reply();
        }),
      ],
    });
    expect(
      await screen.findByText("No Entra users yet.", {}, LOAD),
    ).toBeVisible();
    async function open() {
      await actor.click(screen.getByRole("button", { name: "New" }));
      await actor.click(
        await screen.findByRole("menuitem", { name: "Register Entra user" }),
      );
      return screen.findByRole("dialog", { name: "Register Entra user" });
    }
    const dialog = await open();
    await useObjectId(actor, dialog);
    await actor.type(within(dialog).getByLabelText("Object ID"), OID);
    const submit = within(dialog).getByRole("button", {
      name: "Register Entra user",
    });
    await actor.click(submit);
    expect(
      await within(dialog).findByText("This user is already registered."),
    ).toBeVisible();
    await actor.click(submit);
    expect(await within(dialog).findByText(ENTRA_UNREACHABLE)).toBeVisible();
    expect(
      within(dialog).queryByText("This user is already registered."),
    ).toBeNull();
    await actor.click(submit);
    expect(await within(dialog).findByText("No such object.")).toBeVisible();
    expect(within(dialog).queryByText(ENTRA_UNREACHABLE)).toBeNull();

    // Go back clears the form; it opens empty next time.
    await actor.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    const again = await open();
    await useObjectId(actor, again);
    expect(within(again).getByLabelText("Object ID")).toHaveValue("");
    expect(within(again).queryByText("No such object.")).toBeNull();
  });

  it("shows the failed source and keeps the loaded rows", async () => {
    const actor = userEvent.setup();
    let fail = true;
    renderApp("/admin/users", {
      folders,
      ...ENTRA,
      handlers: [
        http.get("*/api/v1/identity/users", () => page([localUser()])),
        http.get("*/api/v1/identity/entra/users", () =>
          fail
            ? HttpResponse.json(
                {
                  type: "urn:dc:problem:internal",
                  title: "Directory read failed.",
                  status: 500,
                },
                {
                  status: 500,
                  headers: { "Content-Type": "application/problem+json" },
                },
              )
            : page([entraUser()]),
        ),
      ],
    });
    expect(
      await screen.findByText("Entra users could not be loaded", {}, LOAD),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "Alice Demo" })).toBeVisible();
    fail = false;
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("link", { name: "Erin Entra" }),
    ).toBeVisible();
    expect(screen.queryByText("Entra users could not be loaded")).toBeNull();
  });

  it("shows the Source badge and the Users breadcrumb on both detail pages", async () => {
    renderApp("/admin/users/u-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/users/u-1", () =>
          HttpResponse.json(localUser()),
        ),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { level: 1, name: "Alice Demo" },
        LOAD,
      ),
    ).toBeVisible();
    const local = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(local)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Home", "Administration", "Users", "Alice Demo"]);
    expect(within(local).getByRole("link", { name: "Users" })).toHaveAttribute(
      "href",
      "/admin/users",
    );
    expect(screen.getByText("Local")).toBeVisible();
  });

  it("shows the Entra source badge, the Users breadcrumb and a Users back link", async () => {
    renderApp("/admin/entra/users/eu-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/entra/users/eu-1", () =>
          HttpResponse.json(entraUser()),
        ),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { level: 1, name: "Erin Entra" },
        LOAD,
      ),
    ).toBeVisible();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(crumbs)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Home", "Administration", "Users", "Erin Entra"]);
    expect(within(crumbs).getByRole("link", { name: "Users" })).toHaveAttribute(
      "href",
      "/admin/users",
    );
    expect(screen.getByText("Entra")).toBeVisible();
  });
});

describe("groups", () => {
  it("merges local and Entra groups with Source, Owner and Members", async () => {
    renderApp("/admin/groups", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/groups", () =>
          page([
            localGroup(),
            localGroup({
              id: "grp-2",
              name: "orphans",
              owner_folder_id: "ghost-id",
            }),
          ]),
        ),
        http.get("*/api/v1/identity/groups/grp-1/members", () =>
          page([{}, {}, {}]),
        ),
        http.get("*/api/v1/identity/entra/groups", () => page([entraGroup()])),
      ],
    });
    const table = await screen.findByRole("table", { name: "Groups" }, LOAD);
    await within(table).findByRole("link", { name: "Data Engineers" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent.trim()),
    ).toEqual(["Name", "Source", "Owner", "Members"]);
    expect(
      within(table)
        .getAllByRole("rowheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Data Engineers", "demo-editors", "orphans"]);
    const rowOf = (name: string) =>
      within(table).getByRole("link", { name }).closest("tr");
    expect(rowOf("Data Engineers")).toHaveTextContent("Entra");
    expect(rowOf("Data Engineers")).toHaveTextContent("ROOT (global)");
    expect(rowOf("Data Engineers")).toHaveTextContent("Managed in Entra");
    expect(
      within(table).getByRole("link", { name: "Data Engineers" }),
    ).toHaveAttribute("href", "/admin/entra/groups/eg-1");
    expect(rowOf("demo-editors")).toHaveTextContent("Local");
    expect(rowOf("demo-editors")).toHaveTextContent("acme");
    await waitFor(() => {
      expect(rowOf("demo-editors")).toHaveTextContent("3");
    });
    expect(
      within(table).getByRole("link", { name: "demo-editors" }),
    ).toHaveAttribute("href", "/admin/groups/grp-1");
    // An owner the caller cannot see falls back to its id.
    expect(rowOf("orphans")).toHaveTextContent("ghost-id");
  });

  it("counts members at most four requests at a time, pages a count and caps it", async () => {
    let inFlight = 0;
    let peak = 0;
    const requested: string[] = [];
    renderApp("/admin/groups", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups", () =>
          page(
            Array.from({ length: 8 }, (_, i) =>
              localGroup({ id: `grp-${String(i)}`, name: `team-${String(i)}` }),
            ),
          ),
        ),
        http.get(
          "*/api/v1/identity/groups/:id/members",
          async ({ params, request }) => {
            const id = String(params.id);
            const cursor = new URL(request.url).searchParams.get("cursor");
            requested.push(`${id}:${cursor ?? ""}`);
            inFlight += 1;
            peak = Math.max(peak, inFlight);
            await delay(20);
            inFlight -= 1;
            if (id === "grp-1")
              return cursor === null ? page([{}, {}], "p2") : page([{}]);
            // Never ends: capped at ten pages of 100.
            if (id === "grp-2") return page([{}], `next-${cursor ?? "0"}`);
            if (id === "grp-3")
              return HttpResponse.json(
                {
                  type: "urn:dc:problem:internal",
                  title: "Count failed",
                  status: 500,
                },
                {
                  status: 500,
                  headers: { "Content-Type": "application/problem+json" },
                },
              );
            return page([]);
          },
        ),
      ],
    });
    const table = await screen.findByRole("table", { name: "Groups" }, LOAD);
    const rowOf = (name: string) =>
      within(table).getByRole("link", { name }).closest("tr");
    await waitFor(() => {
      expect(rowOf("team-1")).toHaveTextContent("3");
      expect(rowOf("team-2")).toHaveTextContent("1,000+");
      expect(rowOf("team-3")).toHaveTextContent("Not available");
      expect(rowOf("team-0")).toHaveTextContent("0");
    }, LOAD);
    expect(peak).toBeLessThanOrEqual(4);
    expect(peak).toBeGreaterThan(1);
    expect(requested.filter((r) => r.startsWith("grp-2:"))).toHaveLength(10);
    // A failed count is not an alert.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not count members for a view of Entra groups only", async () => {
    const counted: string[] = [];
    renderApp("/admin/groups?source=entra", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/groups", () => {
          counted.push("local list");
          return page([localGroup()]);
        }),
        http.get("*/api/v1/identity/groups/:id/members", () => {
          counted.push("members");
          return page([]);
        }),
        http.get("*/api/v1/identity/entra/groups", () => page([entraGroup()])),
      ],
    });
    expect(
      await screen.findByRole("link", { name: "Data Engineers" }, LOAD),
    ).toBeVisible();
    expect(screen.queryByRole("link", { name: "demo-editors" })).toBeNull();
    expect(counted).toEqual([]);
  });

  it("shows Entra only with entraEnabled and entra:read or entra:manage", async () => {
    const requested: string[] = [];
    const entraList = http.get("*/api/v1/identity/entra/groups", () => {
      requested.push("entra");
      return page([entraGroup()]);
    });
    // Entra off: never requested, no Source filter, no register entry.
    const off = renderApp("/admin/groups?source=entra", {
      folders,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([localGroup()])),
        entraList,
      ],
    });
    expect(
      await screen.findByRole("link", { name: "demo-editors" }, LOAD),
    ).toBeVisible();
    expect(screen.queryByRole("group", { name: "Source" })).toBeNull();
    const actor = userEvent.setup();
    await actor.click(screen.getByRole("button", { name: "New" }));
    expect(
      screen.getAllByRole("menuitem").map((item) => item.textContent),
    ).toEqual(["Create local group"]);
    expect(requested).toEqual([]);
    off.queryClient.clear();
  });

  it("hides the Entra list from a caller holding neither entra:read nor entra:manage", async () => {
    const requested: string[] = [];
    renderApp("/admin/groups", {
      folders,
      ...ENTRA,
      me: makeMe({ global_permissions: ["audit:list", "group:list"] }),
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([localGroup()])),
        http.get("*/api/v1/identity/entra/groups", () => {
          requested.push("entra");
          return page([entraGroup()]);
        }),
      ],
    });
    expect(
      await screen.findByRole("link", { name: "demo-editors" }, LOAD),
    ).toBeVisible();
    expect(screen.queryByRole("group", { name: "Source" })).toBeNull();
    expect(requested).toEqual([]);
  });

  it("filters by source and search and keeps them in the URL", async () => {
    const actor = userEvent.setup();
    const { router } = renderApp("/admin/groups", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([localGroup()])),
        http.get("*/api/v1/identity/entra/groups", () => page([entraGroup()])),
      ],
    });
    await screen.findByRole("link", { name: "Data Engineers" }, LOAD);
    const source = screen.getByRole("group", { name: "Source" });
    await actor.click(within(source).getByRole("button", { name: "Entra" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ source: "entra" });
    });
    expect(screen.queryByRole("link", { name: "demo-editors" })).toBeNull();
    await actor.type(screen.getByLabelText("Search groups"), "ROOT");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        source: "entra",
        q: "ROOT",
      });
    });
    // Owner text is searched.
    expect(screen.getByRole("link", { name: "Data Engineers" })).toBeVisible();
    await actor.clear(screen.getByLabelText("Search groups"));
    await actor.type(screen.getByLabelText("Search groups"), OID.slice(0, 8));
    expect(screen.getByRole("link", { name: "Data Engineers" })).toBeVisible();
    await actor.clear(screen.getByLabelText("Search groups"));
    await actor.type(screen.getByLabelText("Search groups"), "nothing-like-it");
    expect(screen.getByText("No groups match the search.")).toBeVisible();
    // Groups have no active flag, so no "Hide deactivated".
    expect(screen.queryByLabelText("Hide deactivated")).toBeNull();
  });

  it("sorts by name, source and owner", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/groups", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/groups", () =>
          page([localGroup({ id: "g-b", name: "Zulu" })]),
        ),
        http.get("*/api/v1/identity/entra/groups", () =>
          page([entraGroup({ id: "e-a", display_name: "alpha" })]),
        ),
      ],
    });
    const table = await screen.findByRole("table", { name: "Groups" }, LOAD);
    await within(table).findByRole("link", { name: "alpha" });
    expect(await sortRowsBy(actor, table, "Name")).toEqual(["alpha", "Zulu"]);
    expect(await sortRowsBy(actor, table, "Source")).toEqual(["alpha", "Zulu"]);
    // "acme" before "ROOT (global)".
    expect(await sortRowsBy(actor, table, "Owner")).toEqual(["Zulu", "alpha"]);
  });

  it("shows the empty state with one New menu in the header, or without it when no owner can be offered", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/groups", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([])),
        http.get("*/api/v1/identity/entra/groups", () => page([])),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "No groups yet" }, LOAD),
    ).toBeVisible();
    // Only the header offers it: the empty state has no New of its own.
    const buttons = screen.getAllByRole("button", { name: "New" });
    expect(buttons).toHaveLength(1);
    await actor.click(buttons[0] as HTMLElement);
    expect(
      screen.getAllByRole("menuitem").map((item) => item.textContent),
    ).toEqual(["Create local group", "Register Entra group"]);
  });

  it("registers an Entra group under the chosen owner and resets on Escape", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/admin/groups?source=entra", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([])),
        http.get("*/api/v1/identity/entra/groups", () => page([entraGroup()])),
        http.post("*/api/v1/identity/entra/groups", async ({ request }) => {
          bodies.push(await request.json());
          return HttpResponse.json(entraGroup(), { status: 201 });
        }),
      ],
    });
    await screen.findByRole("link", { name: "Data Engineers" }, LOAD);
    async function open() {
      await actor.click(screen.getByRole("button", { name: "New" }));
      await actor.click(
        await screen.findByRole("menuitem", { name: "Register Entra group" }),
      );
      return screen.findByRole("dialog", { name: "Register Entra group" });
    }
    let dialog = await open();
    await useObjectId(actor, dialog);
    await actor.type(within(dialog).getByLabelText("Object ID"), "typed");
    await actor.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    dialog = await open();
    await useObjectId(actor, dialog);
    expect(within(dialog).getByLabelText("Object ID")).toHaveValue("");
    const owner = within(dialog).getByLabelText("Owner");
    expect(
      within(owner)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["ROOT (global)", "Acme"]);
    expect(owner).toHaveValue(ROOT_ID);
    await actor.selectOptions(owner, "acme-id");
    await actor.type(within(dialog).getByLabelText("Object ID"), OID);
    await actor.type(
      within(dialog).getByLabelText("Tenant ID (optional)"),
      TID.toUpperCase(),
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Register Entra group" }),
    );
    await waitFor(() => {
      expect(bodies).toEqual([
        { object_id: OID, tenant_id: TID, owner_folder_id: "acme-id" },
      ]);
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("offers no New menu when the caller may own nothing", async () => {
    let posted = false;
    // An Org admin of an archived Organization: nothing can be owned, so nothing can be created.
    renderApp("/admin/groups?source=entra", {
      folders: [rootFolder, makeFolder({ id: "old", archived: true })],
      ...ENTRA,
      me: makeMe({
        assignments: [
          {
            grant_id: "g",
            folder_id: "old",
            folder_path: "old",
            role: "org_admin",
            via_group_id: null,
          },
        ],
        global_permissions: ["entra:read", "group:list"],
      }),
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([])),
        http.get("*/api/v1/identity/entra/groups", () => page([])),
        http.post("*/api/v1/identity/entra/groups", () => {
          posted = true;
          return HttpResponse.json({}, { status: 201 });
        }),
      ],
    });
    expect(
      await screen.findByText("No Entra groups yet.", {}, LOAD),
    ).toBeVisible();
    // No owner can be offered, so there is no New menu to open the dialog from.
    expect(screen.queryByRole("button", { name: "New" })).toBeNull();
    expect(posted).toBe(false);
  });

  it("shows the failed source and keeps the loaded rows", async () => {
    const actor = userEvent.setup();
    let fail = true;
    renderApp("/admin/groups", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([localGroup()])),
        http.get("*/api/v1/identity/entra/groups", () =>
          fail
            ? HttpResponse.json(
                {
                  type: "urn:dc:problem:internal",
                  title: "Directory read failed.",
                  status: 500,
                },
                {
                  status: 500,
                  headers: { "Content-Type": "application/problem+json" },
                },
              )
            : page([entraGroup()]),
        ),
      ],
    });
    expect(
      await screen.findByText("Entra groups could not be loaded", {}, LOAD),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "demo-editors" })).toBeVisible();
    fail = false;
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("link", { name: "Data Engineers" }),
    ).toBeVisible();
  });

  it("pages both lists behind one Load more", async () => {
    const actor = userEvent.setup();
    const cursors: string[] = [];
    renderApp("/admin/groups", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/groups", ({ request }) => {
          const cursor = new URL(request.url).searchParams.get("cursor");
          cursors.push(`local:${cursor ?? ""}`);
          return cursor === null
            ? page([localGroup()], "l2")
            : page([localGroup({ id: "grp-9", name: "late-local" })]);
        }),
        http.get("*/api/v1/identity/entra/groups", ({ request }) => {
          const cursor = new URL(request.url).searchParams.get("cursor");
          cursors.push(`entra:${cursor ?? ""}`);
          return cursor === null
            ? page([entraGroup()], "e2")
            : page([entraGroup({ id: "eg-9", display_name: "Late Entra" })]);
        }),
      ],
    });
    await screen.findByRole("link", { name: "Data Engineers" }, LOAD);
    await actor.click(
      screen.getByRole("button", { name: "Load more results" }),
    );
    expect(
      await screen.findByRole("link", { name: "Late Entra" }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "late-local" })).toBeVisible();
    expect(cursors.sort()).toEqual([
      "entra:",
      "entra:e2",
      "local:",
      "local:l2",
    ]);
    expect(
      screen.queryByRole("button", { name: "Load more results" }),
    ).toBeNull();
  });

  it("opens the create dialog from the New menu", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/groups", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([localGroup()])),
      ],
    });
    await screen.findByRole("link", { name: "demo-editors" }, LOAD);
    await actor.click(screen.getByRole("button", { name: "New" }));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Create local group" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Create local group" }),
    ).toBeVisible();
  });
});

describe("service accounts", () => {
  it("merges service accounts and Entra service principals with Source, Owner and Status", async () => {
    renderApp("/admin/service-accounts", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/service-accounts", () =>
          page([
            serviceAccount(),
            serviceAccount({
              id: "sa-2",
              name: "old-job",
              is_active: false,
              owner_folder_id: "gone-id",
            }),
          ]),
        ),
        http.get("*/api/v1/identity/entra/service-principals", () =>
          page([entraSp()]),
        ),
      ],
    });
    const table = await screen.findByRole(
      "table",
      { name: "Service accounts" },
      LOAD,
    );
    await within(table).findByRole("link", { name: "Ingest App" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent.trim()),
    ).toEqual(["Name", "Source", "Owner", "Status"]);
    expect(
      within(table)
        .getAllByRole("rowheader")
        .map((cell) => cell.textContent),
    ).toEqual(["demo-pipeline", "Ingest App", "old-job"]);
    const rowOf = (name: string) =>
      within(table).getByRole("link", { name }).closest("tr");
    expect(rowOf("Ingest App")).toHaveTextContent("Entra");
    expect(rowOf("Ingest App")).toHaveTextContent("Active");
    expect(
      within(table).getByRole("link", { name: "Ingest App" }),
    ).toHaveAttribute("href", "/admin/entra/service-principals/sp-1");
    expect(rowOf("demo-pipeline")).toHaveTextContent("Local");
    expect(
      within(table).getByRole("link", { name: "demo-pipeline" }),
    ).toHaveAttribute("href", "/admin/service-accounts/sa-1");
    expect(rowOf("old-job")).toHaveTextContent("Deactivated");
    expect(rowOf("old-job")).toHaveTextContent("gone-id");
    const actor = userEvent.setup();
    expect(await sortRowsBy(actor, table, "Name")).toEqual([
      "demo-pipeline",
      "Ingest App",
      "old-job",
    ]);
    expect(await sortRowsBy(actor, table, "Name")).toEqual([
      "old-job",
      "Ingest App",
      "demo-pipeline",
    ]);
  });

  it("hides deactivated rows and searches by name, owner or object ID", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/service-accounts", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/service-accounts", () =>
          page([
            serviceAccount({ id: "sa-2", name: "old-job", is_active: false }),
          ]),
        ),
        http.get("*/api/v1/identity/entra/service-principals", () =>
          page([
            entraSp(),
            entraSp({
              id: "sp-2",
              display_name: "Old App",
              is_active: false,
              object_id: "9a8b7c6d-1a2b-4c3d-8e9f-0123456789ab",
            }),
          ]),
        ),
      ],
    });
    await screen.findByRole("link", { name: "Ingest App" }, LOAD);
    await actor.click(screen.getByLabelText("Hide deactivated"));
    expect(screen.queryByRole("link", { name: "old-job" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Old App" })).toBeNull();
    await actor.click(screen.getByLabelText("Hide deactivated"));
    await actor.type(
      screen.getByLabelText("Search service accounts"),
      "9a8b7c6d",
    );
    expect(screen.getByRole("link", { name: "Old App" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Ingest App" })).toBeNull();
  });

  it("registers a service principal under the default owner", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/admin/service-accounts?source=entra", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/service-accounts", () => page([])),
        http.get("*/api/v1/identity/entra/service-principals", () =>
          page([entraSp()]),
        ),
        http.post(
          "*/api/v1/identity/entra/service-principals",
          async ({ request }) => {
            bodies.push(await request.json());
            return HttpResponse.json(entraSp(), { status: 201 });
          },
        ),
      ],
    });
    await screen.findByRole("link", { name: "Ingest App" }, LOAD);
    await actor.click(screen.getByRole("button", { name: "New" }));
    expect(
      screen.getAllByRole("menuitem").map((item) => item.textContent),
    ).toEqual(["Create service account", "Register Entra service principal"]);
    await actor.click(
      screen.getByRole("menuitem", {
        name: "Register Entra service principal",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Register Entra service principal",
    });
    await useObjectId(actor, dialog);
    await actor.type(within(dialog).getByLabelText("Object ID"), OID);
    await actor.click(
      within(dialog).getByRole("button", {
        name: "Register Entra service principal",
      }),
    );
    await waitFor(() => {
      expect(bodies).toEqual([
        { object_id: OID, tenant_id: null, owner_folder_id: ROOT_ID },
      ]);
    });
  });

  it("never asks for Entra service principals when Entra is not configured", async () => {
    const requested: string[] = [];
    renderApp("/admin/service-accounts?source=entra", {
      folders,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/service-accounts", () =>
          page([serviceAccount()]),
        ),
        http.get("*/api/v1/identity/entra/service-principals", () => {
          requested.push("entra");
          return page([entraSp()]);
        }),
      ],
    });
    expect(
      await screen.findByRole("link", { name: "demo-pipeline" }, LOAD),
    ).toBeVisible();
    expect(screen.queryByRole("group", { name: "Source" })).toBeNull();
    expect(requested).toEqual([]);
  });

  it("shows the empty state with one New menu in the header", async () => {
    renderApp("/admin/service-accounts", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/service-accounts", () => page([])),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { name: "No service accounts yet" },
        LOAD,
      ),
    ).toBeVisible();
    expect(screen.getAllByRole("button", { name: "New" })).toHaveLength(1);
  });
});

describe("the old Entra page", () => {
  it.each([
    ["/admin/entra", "/admin/users"],
    ["/admin/entra?tab=users", "/admin/users"],
    ["/admin/entra?tab=groups", "/admin/groups"],
    ["/admin/entra?tab=service-principals", "/admin/service-accounts"],
    ["/admin/entra?tab=elsewhere", "/admin/users"],
  ])("sends %s to %s filtered to Entra", async (from, to) => {
    const { router } = renderApp(from, {
      folders,
      ...ENTRA,
      me: entraAdmin,
    });
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(to);
    });
    expect(router.state.location.search).toEqual({ source: "entra" });
    // The merged page rendered: one frame heading, never the old tabs.
    expect(
      await screen.findByRole("heading", { level: 1 }, LOAD),
    ).toBeVisible();
    expect(screen.queryByRole("tab")).toBeNull();
  });
});

describe("the name rule on create dialogs", () => {
  const NAME_RULE =
    "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.";

  it("refuses an emoji in a new local group name", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/groups", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([localGroup()])),
      ],
    });
    await screen.findByRole("link", { name: "demo-editors" }, LOAD);
    await actor.click(screen.getByRole("button", { name: "New" }));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Create local group" }),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Create local group" }),
    );
    await actor.type(dialog.getByLabelText("Name"), "ops 😀");
    expect(dialog.getByText(NAME_RULE)).toBeVisible();
  });

  it("refuses an emoji in a new service account name", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/service-accounts", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/service-accounts", () =>
          page([serviceAccount()]),
        ),
      ],
    });
    await screen.findByRole("button", { name: "New" }, LOAD);
    await actor.click(screen.getByRole("button", { name: "New" }));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Create service account" }),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Create service account" }),
    );
    await actor.type(dialog.getByLabelText("Name"), "etl 😀");
    expect(dialog.getByText(NAME_RULE)).toBeVisible();
  });
});
