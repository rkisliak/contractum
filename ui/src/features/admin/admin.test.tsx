// Access administration and identities (Phase 12) through the real router: the principal-id
// builder, expiry helpers, creating a user, the folder Access tab (role select fed only by
// grantable_roles, the grant body, refusals), group member refusals, a service-account key shown
// once and absent from the query cache, and a read-only PAT.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { PERMISSION_IDS } from "@/features/access/permissionRegistry";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, makeMe, renderApp, rootFolder } from "@/test/renderApp";
import {
  isValidPrincipalId,
  principalIdOf,
  principalTypeLabel,
} from "./principal";
import { dateInDays, daysUntil, expiresAtFor, ownerOptions } from "./shared";

afterEach(() => {
  tokenStore.clear();
});

const UUID = "0b5c3f8e-1a2b-4c3d-8e9f-0123456789ab";
const TID = "11111111-2222-4333-8444-555555555555";

const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const ledger = makeFolder({
  id: "ledger-id",
  parent_id: "acme-id",
  kind: "team",
  slug: "ledger",
  display_name: "Ledger",
  path: "acme/ledger",
});
const folders = [rootFolder, acme, ledger];

function problem(status: number, slug: string, detail: string) {
  return HttpResponse.json(
    { type: `urn:dc:problem:${slug}`, title: "Refused", status, detail },
    {
      status,
      headers: { "Content-Type": "application/problem+json" },
    },
  );
}

const user = {
  id: UUID,
  principal_id: `local:${UUID}`,
  username: "demo-alice",
  display_name: "Alice Demo",
  email: null,
  is_active: true,
  must_rotate: false,
  created_by: "local:admin",
  created_at: "2026-11-01T10:00:00Z",
  updated_by: "local:admin",
  updated_at: "2026-11-01T10:00:00Z",
};

const empty = { items: [], next_cursor: null };

/** The principal lists the picker and the name lookup read. */
const principalLists = [
  http.get("*/api/v1/identity/users", () =>
    HttpResponse.json({ items: [user], next_cursor: null }),
  ),
  http.get("*/api/v1/identity/groups", () => HttpResponse.json(empty)),
  http.get("*/api/v1/identity/service-accounts", () =>
    HttpResponse.json(empty),
  ),
  http.get("*/api/v1/identity/entra/users", () => HttpResponse.json(empty)),
  http.get("*/api/v1/identity/entra/groups", () => HttpResponse.json(empty)),
  http.get("*/api/v1/identity/entra/service-principals", () =>
    HttpResponse.json(empty),
  ),
];

describe("principal ids", () => {
  it("builds each documented shape and validates it", () => {
    expect(principalIdOf.localUser(UUID)).toBe(`local:${UUID}`);
    expect(isValidPrincipalId("user", principalIdOf.localUser(UUID))).toBe(
      true,
    );
    expect(isValidPrincipalId("user", principalIdOf.entraUser(TID, UUID))).toBe(
      true,
    );
    expect(
      isValidPrincipalId("local_group", principalIdOf.localGroup(UUID)),
    ).toBe(true);
    expect(isValidPrincipalId("local_group", `local:${UUID}`)).toBe(false);
    expect(
      isValidPrincipalId("entra_group", principalIdOf.entraGroup(UUID)),
    ).toBe(true);
    expect(
      isValidPrincipalId(
        "service_principal",
        principalIdOf.servicePrincipal(TID, UUID),
      ),
    ).toBe(true);
    expect(
      isValidPrincipalId("service_account", principalIdOf.serviceAccount(UUID)),
    ).toBe(true);
    expect(isValidPrincipalId("service_account", UUID)).toBe(false);
    expect(principalTypeLabel("local_group")).toBe("Local group");
    expect(principalTypeLabel("mystery")).toBe("Mystery");
    expect(principalTypeLabel("some_new_type")).toBe("Some new type");
    // A user reads by where it comes from; the id tells local from Entra.
    expect(principalTypeLabel("user", principalIdOf.localUser(UUID))).toBe(
      "Local user",
    );
    expect(principalTypeLabel("user", principalIdOf.entraUser(TID, UUID))).toBe(
      "Entra user",
    );
    expect(principalTypeLabel("user")).toBe("User");
    expect(principalTypeLabel("entra_user")).toBe("Entra user");
    expect(principalTypeLabel("service_principal")).toBe(
      "Entra service principal",
    );
  });
});

describe("expiry helpers", () => {
  const now = new Date(2026, 10, 15, 12, 0, 0);
  it("defaults, caps and rejects out-of-range dates", () => {
    expect(dateInDays(30, now)).toBe("2026-12-15");
    expect(expiresAtFor(dateInDays(30, now), now)).not.toBeNull();
    expect(expiresAtFor(dateInDays(365, now), now)).not.toBeNull();
    expect(expiresAtFor(dateInDays(366, now), now)).toBeNull();
    expect(expiresAtFor(dateInDays(-1, now), now)).toBeNull();
    expect(expiresAtFor("not a date", now)).toBeNull();
    expect(daysUntil(new Date(2026, 10, 25, 12).toISOString(), now)).toBe(10);
  });

  it("offers ROOT only to a global principal", () => {
    const me = {
      global_permissions: [],
      assignments: [
        {
          grant_id: "g",
          folder_id: "acme-id",
          folder_path: "acme",
          role: "org_admin",
          via_group_id: null,
        },
      ],
    } as unknown as Parameters<typeof ownerOptions>[0];
    expect(ownerOptions(me, folders, "group:create")).toEqual([
      { value: "acme-id", label: "Acme" },
    ]);
  });
});

describe("users", () => {
  it("lists users and hides the New menu without user:create", async () => {
    let body: unknown = null;
    renderApp("/admin/users", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/users", () =>
          HttpResponse.json({ items: [user], next_cursor: null }),
        ),
        http.post("*/api/v1/identity/users", async ({ request }) => {
          body = await request.json();
          return HttpResponse.json({ ...user, id: "new-id" }, { status: 201 });
        }),
        http.get("*/api/v1/identity/users/new-id", () =>
          HttpResponse.json({ ...user, id: "new-id", username: "demo-bob" }),
        ),
      ],
    });
    // The list waits for /me (user:read gates the request), so allow for a loaded runner.
    expect(
      await screen.findByRole(
        "link",
        { name: "Alice Demo" },
        { timeout: 4000 },
      ),
    ).toBeVisible();
    // The default test principal lacks user:create; the API still decides.
    expect(screen.queryByRole("button", { name: "New" })).toBeNull();
    expect(body).toBeNull();
  });

  it("creates a user as a Global admin", async () => {
    const actor = userEvent.setup();
    let body: Record<string, unknown> | null = null;
    renderApp("/admin/users", {
      folders,
      me: makeMe({
        global_permissions: ["user:read", "user:create", "user:deactivate"],
      }),
      handlers: [
        http.get("*/api/v1/identity/users", () =>
          HttpResponse.json({ items: [user], next_cursor: null }),
        ),
        http.post("*/api/v1/identity/users", async ({ request }) => {
          body = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json({ ...user, id: "new-id" }, { status: 201 });
        }),
        http.get("*/api/v1/identity/users/new-id", () =>
          HttpResponse.json({ ...user, id: "new-id", username: "demo-bob" }),
        ),
      ],
    });
    await actor.click(await screen.findByRole("button", { name: "New" }));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Create local user" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Create local user",
    });
    await actor.type(within(dialog).getByLabelText("Username"), "demo-bob");
    await actor.type(within(dialog).getByLabelText("Display name"), "Bob");
    await actor.type(
      within(dialog).getByLabelText("Initial password"),
      "a-long-passphrase-42",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Create local user" }),
    );
    await waitFor(() => {
      expect(body).toEqual({
        username: "demo-bob",
        display_name: "Bob",
        password: "a-long-passphrase-42",
        email: null,
      });
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Alice Demo" }),
    ).toBeVisible();
  });
});

describe("folder access tab", () => {
  function accessHandlers(grantBody: { value: unknown }, refuse = false) {
    return [
      ...principalLists,
      http.get("*/api/v1/identity/me/capabilities", ({ request }) =>
        HttpResponse.json({
          folder_id: new URL(request.url).searchParams.get("folder_id"),
          roles: ["org_admin"],
          permissions: ["grant:manage", "grant:read", "folder:set-block"],
          inheritance_blocked: false,
          grantable_roles: ["team_editor", "team_reader"],
        }),
      ),
      http.get("*/api/v1/identity/folders/ledger-id/grants", () =>
        HttpResponse.json({
          items: [
            {
              id: "grant-1",
              folder_id: "ledger-id",
              principal_type: "user",
              principal_id: `local:${UUID}`,
              role: "team_reader",
              created_by: "local:admin",
              created_at: "2026-11-02T10:00:00Z",
            },
          ],
          next_cursor: null,
        }),
      ),
      http.get("*/api/v1/identity/folders/ledger-id/effective-access", () =>
        HttpResponse.json({
          folder_id: "ledger-id",
          roles: ["org_admin"],
          permissions: ["grant:manage", "grant:read"],
          inheritance_blocked: false,
          inheritance_blocked_at: null,
        }),
      ),
      http.get("*/api/v1/identity/folders/ledger-id/grantable-groups", () =>
        HttpResponse.json({ items: [] }),
      ),
      http.post(
        "*/api/v1/identity/folders/ledger-id/grants",
        async ({ request }) => {
          grantBody.value = await request.json();
          if (refuse)
            return problem(
              403,
              "grant-ceiling-exceeded",
              "You cannot grant a role above your own.",
            );
          return HttpResponse.json({}, { status: 201 });
        },
      ),
    ];
  }

  it("lists grants with names, offers only grantable roles and grants", async () => {
    const actor = userEvent.setup();
    const grantBody = { value: null as unknown };
    renderApp("/folders/ledger-id/access", {
      folders,
      handlers: accessHandlers(grantBody),
    });
    expect(
      await screen.findByRole("heading", { name: "Grants at this folder" }),
    ).toBeVisible();
    expect(await screen.findByText("Alice Demo (demo-alice)")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Inheritance" })).toBeVisible();
    expect(
      await screen.findByRole("heading", { name: "Your effective access" }),
    ).toBeVisible();

    await actor.click(screen.getByRole("button", { name: "Grant access" }));
    const dialog = await screen.findByRole("dialog", { name: "Grant access" });
    const role = within(dialog).getByLabelText("Role");
    const options = within(role)
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(options).toEqual(["Choose a role", "Team editor", "Team reader"]);
    // Search-only: nothing is listed until two characters are typed.
    expect(
      within(dialog).getByText(
        "Type at least 2 characters: name, username, email or object ID",
      ),
    ).toBeVisible();
    expect(
      within(dialog).queryByRole("option", { name: /Alice Demo/ }),
    ).toBeNull();
    await actor.type(within(dialog).getByLabelText("Search users"), "ali");
    await actor.click(
      await within(dialog).findByRole("option", { name: /Alice Demo/ }),
    );
    await actor.selectOptions(role, "team_editor");
    expect(
      within(dialog).getByText(/Grant/, { selector: "p" }),
    ).toHaveTextContent(
      "Grant Team editor to Alice Demo (local user) at acme/ledger",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Grant access" }),
    );
    await waitFor(() => {
      expect(grantBody.value).toEqual({
        principal_type: "user",
        principal_id: `local:${UUID}`,
        role: "team_editor",
      });
    });
  });

  it("grants to a local user by username when the caller may not list users", async () => {
    const actor = userEvent.setup();
    const grantBody = { value: null as unknown };
    const listed: string[] = [];
    renderApp("/folders/ledger-id/access", {
      folders,
      // An Org admin: no root-level user:read, so no user list is ever requested.
      me: makeMe({ global_permissions: ["audit:list", "group:list"] }),
      handlers: [
        http.get("*/api/v1/identity/users", () => {
          listed.push("users");
          return problem(403, "forbidden", "You may not list users.");
        }),
        http.get("*/api/v1/identity/entra/users", () => {
          listed.push("entra/users");
          return problem(403, "forbidden", "You may not list users.");
        }),
        ...accessHandlers(grantBody),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Grant access" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Grant access" });
    await actor.type(within(dialog).getByLabelText("Search users"), " bob ");
    const named = await within(dialog).findByRole("option", {
      name: /Use local username/,
    });
    expect(named).toHaveAccessibleName(/bob/);
    expect(
      within(dialog).getByText("The server checks that this username exists"),
    ).toBeVisible();
    expect(
      within(dialog).queryByLabelText("Or enter a local username"),
    ).toBeNull();
    await actor.click(named);
    await actor.selectOptions(
      within(dialog).getByLabelText("Role"),
      "team_editor",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Grant access" }),
    );
    await waitFor(() => {
      expect(grantBody.value).toEqual({
        principal_type: "user",
        username: "bob",
        role: "team_editor",
      });
    });
    expect(listed).toEqual([]);
  });

  it("explains effective access in plain language", async () => {
    const actor = userEvent.setup();
    renderApp("/folders/ledger-id/access", {
      folders,
      me: makeMe({
        assignments: [
          {
            grant_id: "g-acme",
            folder_id: "acme-id",
            folder_path: "acme",
            role: "org_admin",
            via_group_id: null,
          },
        ],
      }),
      handlers: accessHandlers({ value: null }),
    });
    const card = (
      await screen.findByRole("heading", { name: "Your effective access" })
    ).closest("section");
    if (!card) throw new Error("no effective access card");
    const panel = within(card);
    expect(await panel.findByText("Organization admin")).toBeVisible();
    expect(
      panel.getByText(/Manages this organization/, { selector: "span" }),
    ).toBeVisible();
    for (const link of panel.getAllByRole("link", { name: "Acme" }))
      expect(link).toHaveAttribute("href", "/folders/acme-id");
    expect(panel.getByText(/^Granted on/)).toHaveTextContent("Granted on Acme");
    expect(panel.getByText(/also apply here\./)).toHaveTextContent(
      "Roles granted on Acme and Root also apply here.",
    );
    // Collapsed by default, like for every caller.
    const seeDetails = panel.getByText("See details");
    expect(seeDetails.closest("details")).not.toHaveAttribute("open");
    expect(panel.getByRole("table", { hidden: true })).not.toBeVisible();
    await actor.click(seeDetails);
    const table = panel.getByRole("table", {
      name: "What you can do here and in the folders below",
    });
    const people = within(table).getByRole("button", {
      name: "People & access",
    });
    const header = people.closest("th");
    if (!header) throw new Error("no row header");
    expect(header).toHaveTextContent(/Can't /);
    const cells = within(header.closest("tr") ?? table).getAllByRole("cell");
    // View, Create, Edit, Publish, Delete / Archive, Administer.
    expect(cells.map((cell) => cell.textContent)).toEqual([
      "Partly allowed",
      "—Not allowed",
      "—Not allowed",
      "Not applicable",
      "—Not allowed",
      "Partly allowed",
    ]);
    // The readable labels only when the row is expanded.
    expect(
      within(table).getByText("Grant and revoke access"),
    ).not.toBeVisible();
    expect(people).toHaveAttribute("aria-expanded", "false");
    await actor.click(people);
    expect(people).toHaveAttribute("aria-expanded", "true");
    expect(within(table).getByText("Grant and revoke access")).toBeVisible();
    expect(panel.getByText(/^No access:/)).toHaveTextContent(
      /Contracts & versions/,
    );
    // Raw ids only behind the disclosure.
    const summary = panel.getByText("Show technical permission names");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    await actor.click(summary);
    expect(summary.closest("details")).toHaveAttribute("open");
    expect(panel.getByText("grant:manage")).toBeVisible();
  });

  it("keeps the details collapsed for a full-access caller too", async () => {
    const actor = userEvent.setup();
    renderApp("/folders/ledger-id/access", {
      folders,
      handlers: [
        // Listed first so it wins over the partial-access default below.
        http.get("*/api/v1/identity/folders/ledger-id/effective-access", () =>
          HttpResponse.json({
            folder_id: "ledger-id",
            roles: ["global_admin"],
            permissions: [...PERMISSION_IDS],
            inheritance_blocked: false,
            inheritance_blocked_at: null,
          }),
        ),
        ...accessHandlers({ value: null }),
      ],
    });
    const card = (
      await screen.findByRole("heading", { name: "Your effective access" })
    ).closest("section");
    if (!card) throw new Error("no effective access card");
    const panel = within(card);
    const seeDetails = await panel.findByText("See details");
    expect(seeDetails.closest("details")).not.toHaveAttribute("open");
    expect(panel.getByRole("table", { hidden: true })).not.toBeVisible();
    await actor.click(seeDetails);
    expect(await panel.findByRole("table")).toBeVisible();
  });

  it("leaves out the grant list without grant:read and never asks for it", async () => {
    let grantsRequested = false;
    renderApp("/folders/ledger-id/access", {
      folders,
      permissions: ["folder:read", "contract:read"],
      handlers: [
        http.get("*/api/v1/identity/folders/ledger-id/grants", () => {
          grantsRequested = true;
          return HttpResponse.json({}, { status: 403 });
        }),
        http.get("*/api/v1/identity/folders/ledger-id/effective-access", () =>
          HttpResponse.json({
            folder_id: "ledger-id",
            roles: ["team_editor"],
            permissions: ["folder:read", "contract:read"],
            inheritance_blocked: false,
            inheritance_blocked_at: null,
          }),
        ),
        ...principalLists,
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "Your effective access" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Grants at this folder" }),
    ).toBeNull();
    expect(screen.queryByText("Request ID")).toBeNull();
    expect(grantsRequested).toBe(false);
  });

  it("shows an escalation refusal with the API reason", async () => {
    const actor = userEvent.setup();
    const grantBody = { value: null as unknown };
    renderApp("/folders/ledger-id/access", {
      folders,
      handlers: accessHandlers(grantBody, true),
    });
    await actor.click(
      await screen.findByRole("button", { name: "Grant access" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Grant access" });
    await actor.type(within(dialog).getByLabelText("Search users"), "alice");
    await actor.click(
      await within(dialog).findByRole("option", { name: /Alice Demo/ }),
    );
    await actor.selectOptions(
      within(dialog).getByLabelText("Role"),
      "team_reader",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Grant access" }),
    );
    expect(
      await within(dialog).findByText(
        "You cannot grant a role above your own.",
      ),
    ).toBeVisible();
    expect(within(dialog).getByText("grant-ceiling-exceeded")).toBeVisible();
  });
});

describe("group members", () => {
  it("prefixes a refused member with the API detail", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: [
        ...principalLists,
        http.get("*/api/v1/identity/groups/grp-1", () =>
          HttpResponse.json({
            id: "grp-1",
            owner_folder_id: "acme-id",
            name: "demo-ledger-editors",
            created_by: "local:admin",
            created_at: "2026-11-01T10:00:00Z",
            updated_by: "local:admin",
            updated_at: "2026-11-01T10:00:00Z",
          }),
        ),
        http.get("*/api/v1/identity/groups/grp-1/members", () =>
          HttpResponse.json(empty),
        ),
        http.post("*/api/v1/identity/groups/grp-1/members", () =>
          problem(403, "self-grant-forbidden", "You cannot add yourself."),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "demo-ledger-editors",
      }),
    ).toBeVisible();
    await actor.click(
      await screen.findByRole("button", { name: "Add member" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Add a member to demo-ledger-editors",
    });
    await actor.type(within(dialog).getByLabelText("Search users"), "alice");
    await actor.click(
      await within(dialog).findByRole("option", { name: /Alice Demo/ }),
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Add member" }),
    );
    expect(
      await within(dialog).findByText("Cannot add this member:"),
    ).toBeVisible();
    expect(within(dialog).getByText(/You cannot add yourself\./)).toBeVisible();
  });
});

describe("group members by username", () => {
  it("adds a local user by username", async () => {
    const actor = userEvent.setup();
    const added = { value: null as unknown };
    renderApp("/admin/groups/grp-1", {
      folders,
      // No root-level user:read: the picker offers the typed username instead of a list.
      me: makeMe({ global_permissions: ["audit:list", "group:list"] }),
      handlers: [
        ...principalLists,
        http.get("*/api/v1/identity/groups/grp-1", () =>
          HttpResponse.json({
            id: "grp-1",
            owner_folder_id: "acme-id",
            name: "demo-ledger-editors",
            created_by: "local:admin",
            created_at: "2026-11-01T10:00:00Z",
            updated_by: "local:admin",
            updated_at: "2026-11-01T10:00:00Z",
          }),
        ),
        http.get("*/api/v1/identity/groups/grp-1/members", () =>
          HttpResponse.json(empty),
        ),
        http.post(
          "*/api/v1/identity/groups/grp-1/members",
          async ({ request }) => {
            added.value = await request.json();
            return HttpResponse.json({}, { status: 201 });
          },
        ),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Add member" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Add a member to demo-ledger-editors",
    });
    await actor.type(
      await within(dialog).findByLabelText("Search users"),
      "bob",
    );
    await actor.click(
      await within(dialog).findByRole("option", { name: /Use local username/ }),
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Add member" }),
    );
    await waitFor(() => {
      expect(added.value).toEqual({ username: "bob" });
    });
  });
});

describe("service account keys", () => {
  it("shows a new key once and keeps it out of the query cache", async () => {
    const actor = userEvent.setup();
    const token = "dcp_abc123_secretsecretsecret";
    const keys: unknown[] = [];
    const { queryClient } = renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/service-accounts/sa-1", () =>
          HttpResponse.json({
            id: "sa-1",
            principal_id: "sa:sa-1",
            owner_folder_id: "acme-id",
            name: "demo-pipeline",
            is_active: true,
            created_by: "local:admin",
            created_at: "2026-11-01T10:00:00Z",
            updated_by: "local:admin",
            updated_at: "2026-11-01T10:00:00Z",
          }),
        ),
        http.get("*/api/v1/identity/service-accounts/sa-1/keys", () =>
          HttpResponse.json({ items: keys }),
        ),
        http.post(
          "*/api/v1/identity/service-accounts/sa-1/keys",
          async ({ request }) => {
            const body = (await request.json()) as { name: string };
            keys.push({
              id: "k-1",
              name: body.name,
              expires_at: "2027-02-13T22:59:00Z",
              created_at: "2026-11-15T10:00:00Z",
              revoked_at: null,
              last_used_at: null,
              status: "active",
              expiring_soon: false,
            });
            return HttpResponse.json(
              {
                id: "k-1",
                name: body.name,
                expires_at: "2027-02-13T22:59:00Z",
                created_at: "2026-11-15T10:00:00Z",
                token,
              },
              { status: 201 },
            );
          },
        ),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Create key" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create key" });
    await actor.type(within(dialog).getByLabelText("Name"), "key-1");
    await actor.click(
      within(dialog).getByRole("button", { name: "Create key" }),
    );
    const reveal = await screen.findByRole("dialog", { name: "Your new key" });
    expect(within(reveal).getByLabelText("Key")).toHaveValue(token);
    const close = within(reveal).getByRole("button", {
      name: "Close after storing key",
    });
    expect(close).toBeDisabled();
    await actor.click(within(reveal).getByLabelText("I have stored this key"));
    await actor.click(close);
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Your new key" })).toBeNull();
    });
    expect(
      await screen.findByRole("rowheader", { name: "key-1" }),
    ).toBeVisible();
    const cached = JSON.stringify([
      queryClient
        .getQueryCache()
        .getAll()
        .map((q) => q.state.data),
      queryClient
        .getMutationCache()
        .getAll()
        .map((m) => m.state.data),
    ]);
    expect(cached).not.toContain(token);
    expect(document.body.textContent).not.toContain(token);
  });
});

describe("access tokens", () => {
  it("creates a read-only PAT and offers revoke", async () => {
    const actor = userEvent.setup();
    let body: Record<string, unknown> | null = null;
    renderApp("/account/tokens", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/pats", () => HttpResponse.json(empty)),
        http.post("*/api/v1/identity/pats", async ({ request }) => {
          body = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            {
              id: "p-1",
              name: "laptop",
              read_only: true,
              expires_at: "2026-12-15T22:59:00Z",
              created_at: "2026-11-15T10:00:00Z",
              token: "dcp_x_y",
            },
            { status: 201 },
          );
        }),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "No access tokens yet" }),
    ).toBeVisible();
    expect(
      screen.getByRole("navigation", { name: "Account settings" }),
    ).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Create token" }));
    const dialog = await screen.findByRole("dialog", { name: "Create token" });
    await actor.type(within(dialog).getByLabelText("Name"), "laptop");
    await actor.click(within(dialog).getByLabelText("Read-only"));
    await actor.click(
      within(dialog).getByRole("button", { name: "Create token" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Your new token" }),
    ).toBeVisible();
    expect(body).toMatchObject({ name: "laptop", read_only: true });
    expect(typeof (body as unknown as { expires_at: string }).expires_at).toBe(
      "string",
    );
  });
});

describe("the name rule on user creation", () => {
  it("refuses an emoji in the display name and sends nothing", async () => {
    const actor = userEvent.setup();
    let posted = false;
    renderApp("/admin/users", {
      folders,
      me: makeMe({
        global_permissions: ["user:read", "user:create", "user:deactivate"],
      }),
      handlers: [
        http.get("*/api/v1/identity/users", () =>
          HttpResponse.json({ items: [user], next_cursor: null }),
        ),
        http.post("*/api/v1/identity/users", () => {
          posted = true;
          return HttpResponse.json({}, { status: 201 });
        }),
      ],
    });
    await actor.click(await screen.findByRole("button", { name: "New" }));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Create local user" }),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Create local user" }),
    );
    await actor.type(dialog.getByLabelText("Username"), "demo-bob");
    await actor.type(dialog.getByLabelText("Display name"), "Bob 😀");
    await actor.type(
      dialog.getByLabelText("Initial password"),
      "a-long-passphrase-42",
    );
    expect(
      dialog.getByText(
        "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.",
      ),
    ).toBeVisible();
    await actor.click(
      dialog.getByRole("button", { name: "Create local user" }),
    );
    expect(posted).toBe(false);
  });
});
