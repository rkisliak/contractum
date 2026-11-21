// Entra registrations (list tabs, paging, filters, register dialogs and their refusals), the Entra
// detail pages (deactivate, reactivate, delete, 404, load errors), the shared principal picker
// (every principal type, search, partial and failed directories, a typed Entra group id), the
// folder Access tab (grants, revoke, grant refusals, block inheritance) and the grant-row names.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import type {
  EntraGroup,
  EntraGroupMember,
  EntraServicePrincipal,
  EntraUser,
  Grant,
  LocalGroup,
  LocalUser,
  Me,
  ServiceAccount,
} from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { sortRowsBy } from "@/test/governance-table";
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
const OID2 = "9a8b7c6d-1a2b-4c3d-8e9f-0123456789ab";
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
const ledger = makeFolder({
  id: "ledger-id",
  parent_id: "acme-id",
  kind: "team",
  slug: "ledger",
  display_name: "Ledger",
  path: "acme/ledger",
});
const payments = makeFolder({
  id: "payments-id",
  parent_id: "acme-id",
  kind: "project",
  slug: "payments",
  display_name: "Payments",
  path: "acme/payments",
});
const folders = [rootFolder, acme, ledger, payments];

const page = (items: unknown[], next: string | null = null) =>
  HttpResponse.json({ items, next_cursor: next });

function problem(status: number, slug: string, detail?: string) {
  return HttpResponse.json(
    {
      type: `urn:dc:problem:${slug}`,
      title: `Refused ${String(status)}`,
      status,
      ...(detail ? { detail } : {}),
    },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

const reader = (extra: string[] = []): Me =>
  makeMe({
    global_permissions: ["user:read", "audit:list", "group:list", ...extra],
  });

/** The default test principal holds user:read; this one does not. */
const noUserRead: Me = makeMe({
  global_permissions: ["audit:list", "group:list"],
});

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

function entraSp(
  overrides: Partial<EntraServicePrincipal> = {},
): EntraServicePrincipal {
  return {
    id: "sp-1",
    principal_id: `entra:${TID}:${OID2}`,
    tenant_id: TID,
    object_id: OID2,
    display_name: "Ingest App",
    owner_folder_id: "acme-id",
    is_active: true,
    directly_registered: true,
    ...AUDIT,
    ...overrides,
  };
}

const localUser: LocalUser = {
  id: "u-1",
  principal_id: "local:u-1",
  username: "demo-alice",
  display_name: "Alice Demo",
  email: null,
  is_active: true,
  must_rotate: false,
  ...AUDIT,
};

// ---------------------------------------------------------------- shared helpers

/** Empty Entra lists for the registrations a test does not look at (listed last, so a test's own win). */
const entraDefaults = [
  http.get("*/api/v1/identity/entra/users", () => page([])),
  http.get("*/api/v1/identity/entra/groups", () => page([])),
  http.get("*/api/v1/identity/entra/service-principals", () => page([])),
];

// ---------------------------------------------------------------- Entra details

describe("Entra user detail", () => {
  it("deactivates with type-to-confirm, then reactivates", async () => {
    const actor = userEvent.setup();
    let active = true;
    const posts: string[] = [];
    renderApp("/admin/entra/users/eu-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/entra/users/eu-1", () =>
          HttpResponse.json(
            entraUser({
              is_active: active,
              preferred_username: null,
              registered_directly: false,
            }),
          ),
        ),
        http.post(
          "*/api/v1/identity/entra/users/eu-1/:action",
          ({ params }) => {
            posts.push(String(params.action));
            active = params.action === "reactivate";
            return HttpResponse.json(entraUser({ is_active: active }));
          },
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
    expect(screen.getByText(`entra:${TID}:${OID}`)).toBeVisible();
    expect(screen.getByText("Display name")).toBeVisible();
    expect(
      screen.getByText(
        "Comes from Microsoft Entra ID; change it in Microsoft.",
      ),
    ).toBeVisible();
    expect(screen.getByText(/At first sign‑in,/)).toBeVisible();
    expect(screen.getByText("Not set")).toBeVisible();
    expect(
      within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByRole(
        "link",
        { name: "Users" },
      ),
    ).toHaveAttribute("href", "/admin/users");

    await actor.click(screen.getByRole("button", { name: "Deactivate user" }));
    let dialog = await screen.findByRole("alertdialog", {
      name: "Deactivate Erin Entra?",
    });
    const confirm = within(dialog).getByRole("button", {
      name: "Deactivate Erin Entra",
    });
    expect(confirm).toBeDisabled();
    // Keep closes without a request.
    await actor.click(
      within(dialog).getByRole("button", { name: "Keep Erin Entra" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(posts).toEqual([]);

    await actor.click(screen.getByRole("button", { name: "Deactivate user" }));
    dialog = await screen.findByRole("alertdialog", {
      name: "Deactivate Erin Entra?",
    });
    await actor.type(
      within(dialog).getByLabelText("Type Erin Entra to confirm"),
      "Erin Entra",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Deactivate Erin Entra" }),
    );
    expect(
      await screen.findByRole("button", { name: "Reactivate user" }),
    ).toBeVisible();
    expect(posts).toEqual(["deactivate"]);
    expect(screen.getByText("Deactivated")).toBeVisible();

    await actor.click(screen.getByRole("button", { name: "Reactivate user" }));
    dialog = await screen.findByRole("alertdialog", {
      name: "Reactivate Erin Entra?",
    });
    expect(within(dialog).queryByLabelText(/to confirm/)).toBeNull();
    await actor.click(
      within(dialog).getByRole("button", { name: "Reactivate Erin Entra" }),
    );
    expect(
      await screen.findByRole("button", { name: "Deactivate user" }),
    ).toBeVisible();
    expect(posts).toEqual(["deactivate", "reactivate"]);
  });

  it("keeps the dialog open with the refusal", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/entra/users/eu-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/entra/users/eu-1", () =>
          HttpResponse.json(entraUser({ is_active: false })),
        ),
        http.post("*/api/v1/identity/entra/users/eu-1/reactivate", () =>
          problem(409, "conflict", "Already active elsewhere."),
        ),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Reactivate user" }, LOAD),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Reactivate Erin Entra?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Reactivate Erin Entra" }),
    );
    expect(
      await within(dialog).findByText("Already active elsewhere."),
    ).toBeVisible();
    // The cancel path of the reactivate dialog.
    await actor.click(
      within(dialog).getByRole("button", { name: "Keep Erin Entra" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("renders Not found for a 404 and a ProblemAlert with retry otherwise", async () => {
    renderApp("/admin/entra/users/missing", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/entra/users/missing", () =>
          problem(404, "not-found"),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Not found" }, LOAD),
    ).toBeVisible();
    expect(
      within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByRole(
        "link",
        { name: "Users" },
      ),
    ).toHaveAttribute("href", "/admin/users?source=entra");
  });

  it("retries a failed detail load", async () => {
    const actor = userEvent.setup();
    let fail = true;
    renderApp("/admin/entra/users/eu-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/entra/users/eu-1", () =>
          fail
            ? problem(500, "internal", "Lookup failed.")
            : HttpResponse.json(entraUser()),
        ),
      ],
    });
    expect(await screen.findByText("Lookup failed.", {}, LOAD)).toBeVisible();
    fail = false;
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Erin Entra" }),
    ).toBeVisible();
  });
});

type Member = EntraGroupMember;

function member(overrides: Partial<Member> = {}): Member {
  return {
    object_id: "11111111-1111-4111-8111-111111111111",
    display_name: "Plain Pat",
    secondary: "pat@example.com",
    kind: "user",
    account_enabled: true,
    registered: false,
    principal_id: null,
    record_id: null,
    ...overrides,
  };
}

const membersOk = (items: Member[], extra: Record<string, unknown> = {}) =>
  http.get("*/api/v1/identity/entra/groups/eg-1/members", () =>
    HttpResponse.json({
      items,
      truncated: false,
      total: items.length,
      ...extra,
    }),
  );

async function openGroup(handlers: RequestHandler[]) {
  renderApp("/admin/entra/groups/eg-1", {
    folders,
    handlers: [
      http.get("*/api/v1/identity/entra/groups/eg-1", () =>
        HttpResponse.json(entraGroup({ owner_folder_id: "acme-id" })),
      ),
      ...handlers,
      http.get("*/api/v1/identity/entra/groups", () => page([])),
    ],
  });
  await screen.findByRole(
    "heading",
    { level: 1, name: "Data Engineers" },
    LOAD,
  );
  return screen.findByRole("heading", { level: 2, name: "Members" });
}

describe("Entra group members", () => {
  it("lists the live members with links only for the records that exist", async () => {
    await openGroup([
      membersOk([
        member({
          object_id: "a1111111-1111-4111-8111-111111111111",
          display_name: "Alice Linked",
          secondary: "alice@example.com",
          registered: true,
          principal_id: `entra:${TID}:a1111111-1111-4111-8111-111111111111`,
          record_id: "eu-9",
        }),
        member({
          object_id: "b2222222-2222-4222-8222-222222222222",
          display_name: "Nested Team",
          secondary: "nested@example.com",
          kind: "group",
          account_enabled: null,
        }),
        member({
          object_id: "c3333333-3333-4333-8333-333333333333",
          display_name: "Pipeline App",
          secondary: "app-id-1",
          kind: "service_principal",
          registered: true,
          record_id: "sp-9",
        }),
        member({
          object_id: "d4444444-4444-4444-8444-444444444444",
          display_name: "Dormant Dee",
          account_enabled: false,
        }),
      ]),
    ]);
    expect(
      screen.getByText(
        "Live from Microsoft Entra ID; nested members included.",
      ),
    ).toBeVisible();
    const table = await screen.findByRole("table", {
      name: "Members of this Entra group",
    });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(5);
    const row = (name: string) =>
      rows.find((r) => within(r).queryByText(name) !== null) as HTMLElement;
    expect(
      within(row("Alice Linked")).getByRole("link", { name: "Alice Linked" }),
    ).toHaveAttribute("href", "/admin/entra/users/eu-9");
    expect(row("Alice Linked")).toHaveTextContent("alice@example.com");
    expect(row("Alice Linked")).toHaveTextContent("User");
    expect(row("Alice Linked")).toHaveTextContent("Enabled");
    expect(within(row("Nested Team")).queryByRole("link")).toBeNull();
    expect(row("Nested Team")).toHaveTextContent("Group");
    expect(
      within(row("Pipeline App")).getByRole("link", { name: "Pipeline App" }),
    ).toHaveAttribute("href", "/admin/entra/service-principals/sp-9");
    expect(row("Pipeline App")).toHaveTextContent("Service principal");
    expect(row("Dormant Dee")).toHaveTextContent("Disabled");
    expect(within(row("Dormant Dee")).queryByRole("link")).toBeNull();
  });

  it("says when the group has no members", async () => {
    await openGroup([membersOk([])]);
    expect(
      await screen.findByRole("heading", {
        level: 3,
        name: "This group has no members",
      }),
    ).toBeVisible();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("says how many of the members are shown when the list is cut", async () => {
    await openGroup([
      membersOk(
        [
          member(),
          member({
            object_id: "e5555555-5555-4555-8555-555555555555",
            display_name: "Second",
          }),
        ],
        {
          truncated: true,
          total: 250,
        },
      ),
    ]);
    expect(
      await screen.findByText("Showing the first 2 of 250 members."),
    ).toBeVisible();
  });

  it("shows a warning with Retry loading when the directory is unavailable", async () => {
    const actor = userEvent.setup();
    let calls = 0;
    await openGroup([
      http.get("*/api/v1/identity/entra/groups/eg-1/members", () => {
        calls += 1;
        return calls === 1
          ? problem(503, "service-unavailable")
          : HttpResponse.json({
              items: [member({ display_name: "Back Again" })],
              truncated: false,
              total: 1,
            });
      }),
    ]);
    const alert = await screen.findByRole("status");
    expect(alert).toHaveTextContent(/could not be reached/);
    expect(screen.queryByRole("alert")).toBeNull();
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(await screen.findByText("Back Again")).toBeVisible();
  });

  it("explains a group Entra no longer has", async () => {
    await openGroup([
      http.get("*/api/v1/identity/entra/groups/eg-1/members", () =>
        problem(404, "not-found"),
      ),
    ]);
    expect(await screen.findByText(/no longer has this group/)).toBeVisible();
  });

  it("renders any other failure as a problem with a retry", async () => {
    await openGroup([
      http.get("*/api/v1/identity/entra/groups/eg-1/members", () =>
        problem(500, "internal"),
      ),
    ]);
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(
      screen.getByRole("button", { name: /retry|try again/i }),
    ).toBeVisible();
  });
});

describe("Entra group detail", () => {
  it("deletes the registration and returns to the Entra groups", async () => {
    const actor = userEvent.setup();
    const deleted: string[] = [];
    const { router } = renderApp("/admin/entra/groups/eg-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/entra/groups/eg-1", () =>
          HttpResponse.json(entraGroup({ owner_folder_id: "acme-id" })),
        ),
        membersOk([]),
        http.delete("*/api/v1/identity/entra/groups/eg-1", ({ request }) => {
          deleted.push(request.method);
          return new HttpResponse(null, { status: 204 });
        }),
        http.get("*/api/v1/identity/entra/groups", () => page([])),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { level: 1, name: "Data Engineers" },
        LOAD,
      ),
    ).toBeVisible();
    expect(await screen.findByText("acme")).toBeVisible();
    expect(screen.getByText(TID)).toBeVisible();
    await actor.click(
      screen.getByRole("button", { name: "Delete registration" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Delete the registration of Data Engineers?",
    });
    expect(
      within(dialog).getByText("Type the name exactly to enable Delete."),
    ).toBeVisible();
    await actor.type(
      within(dialog).getByLabelText("Type Data Engineers to confirm"),
      "Data Engineers",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Delete registration" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/admin/groups");
    });
    expect(router.state.location.search).toEqual({ source: "entra" });
    expect(deleted).toEqual(["DELETE"]);
  });

  it("renders Not found with a link back to the Entra groups", async () => {
    const { router } = renderApp("/admin/entra/groups/missing", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/entra/groups/missing", () =>
          problem(404, "not-found"),
        ),
        http.get("*/api/v1/identity/entra/groups", () => page([])),
      ],
    });
    const actor = userEvent.setup();
    await actor.click(
      await screen.findByRole("button", { name: "Back to groups" }, LOAD),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/admin/groups");
    });
    expect(router.state.location.search).toEqual({ source: "entra" });
  });
});

describe("Entra service principal detail", () => {
  it("deactivates and reactivates without typing a name", async () => {
    const actor = userEvent.setup();
    let active = true;
    const posts: string[] = [];
    renderApp("/admin/entra/service-principals/sp-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/entra/service-principals/sp-1", () =>
          HttpResponse.json(entraSp({ is_active: active })),
        ),
        http.post(
          "*/api/v1/identity/entra/service-principals/sp-1/:action",
          ({ params }) => {
            posts.push(String(params.action));
            active = params.action === "reactivate";
            return HttpResponse.json(entraSp({ is_active: active }));
          },
        ),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { level: 1, name: "Ingest App" },
        LOAD,
      ),
    ).toBeVisible();
    expect(screen.getByText(`entra:${TID}:${OID2}`)).toBeVisible();
    expect(await screen.findByText("acme")).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Deactivate" }));
    let dialog = await screen.findByRole("alertdialog", {
      name: "Deactivate Ingest App?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Keep Ingest App" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    await actor.click(screen.getByRole("button", { name: "Deactivate" }));
    dialog = await screen.findByRole("alertdialog", {
      name: "Deactivate Ingest App?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Deactivate Ingest App" }),
    );
    await actor.click(
      await screen.findByRole("button", { name: "Reactivate" }),
    );
    dialog = await screen.findByRole("alertdialog", {
      name: "Reactivate Ingest App?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Keep Ingest App" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(posts).toEqual(["deactivate"]);
    await actor.click(screen.getByRole("button", { name: "Reactivate" }));
    dialog = await screen.findByRole("alertdialog", {
      name: "Reactivate Ingest App?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Reactivate Ingest App" }),
    );
    expect(
      await screen.findByRole("button", { name: "Deactivate" }),
    ).toBeVisible();
    expect(posts).toEqual(["deactivate", "reactivate"]);
  });

  it("renders Not found with a link back to the Entra service principals", async () => {
    const { router } = renderApp("/admin/entra/service-principals/missing", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/entra/service-principals/missing", () =>
          problem(404, "not-found"),
        ),
        ...entraDefaults,
      ],
    });
    const actor = userEvent.setup();
    await actor.click(
      await screen.findByRole(
        "button",
        { name: "Back to service accounts" },
        LOAD,
      ),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/admin/service-accounts");
    });
    expect(router.state.location.search).toEqual({ source: "entra" });
  });
});

// ---------------------------------------------------------------- folder Access tab

const localGroup: LocalGroup = {
  id: "aaaaaaaa-1111-4111-8111-111111111111",
  name: "demo-ledger-editors",
  owner_folder_id: "acme-id",
  ...AUDIT,
};
const serviceAccount: ServiceAccount = {
  id: "sa-1",
  principal_id: "sa:sa-1",
  name: "demo-pipeline",
  owner_folder_id: "acme-id",
  is_active: true,
  ...AUDIT,
};

function grant(overrides: Partial<Grant> & Pick<Grant, "id">): Grant {
  return {
    folder_id: "ledger-id",
    principal_type: "user",
    principal_id: "local:u-1",
    role: "team_reader",
    created_by: "local:u-1",
    created_at: "2026-11-02T10:00:00Z",
    ...overrides,
  };
}

type AccessOptions = {
  perms?: string[];
  parentPerms?: string[];
  grantable?: string[];
  grants?: () => Response;
  effective?: () => Response;
  extra?: RequestHandler[];
};

function accessHandlers({
  perms = ["grant:manage", "grant:read"],
  parentPerms = [],
  grantable = ["team_editor", "team_reader"],
  grants = () => page([grant({ id: "grant-1" })]),
  effective,
  extra = [],
}: AccessOptions = {}) {
  return [
    ...extra,
    http.get("*/api/v1/identity/me/capabilities", ({ request }) => {
      const folderId = new URL(request.url).searchParams.get("folder_id");
      return HttpResponse.json({
        folder_id: folderId,
        roles: ["org_admin"],
        permissions: folderId === "acme-id" ? parentPerms : perms,
        inheritance_blocked: false,
        grantable_roles: grantable,
      });
    }),
    http.get("*/api/v1/identity/folders/:id/grants", () => grants()),
    http.get("*/api/v1/identity/folders/:id/effective-access", ({ params }) =>
      effective
        ? effective()
        : HttpResponse.json({
            folder_id: params.id,
            roles: ["org_admin"],
            permissions: ["grant:manage", "grant:read"],
            inheritance_blocked: false,
            inheritance_blocked_at: null,
          }),
    ),
    http.get("*/api/v1/identity/folders/:id/grantable-groups", () =>
      HttpResponse.json({ items: [localGroup] }),
    ),
    http.get("*/api/v1/identity/users", () => page([localUser])),
    http.get("*/api/v1/identity/groups", () => page([localGroup])),
    http.get("*/api/v1/identity/service-accounts", () =>
      page([serviceAccount]),
    ),
    http.get("*/api/v1/identity/entra/users", () => page([entraUser()])),
    http.get("*/api/v1/identity/entra/groups", () => page([entraGroup()])),
    http.get("*/api/v1/identity/entra/service-principals", () =>
      page([entraSp()]),
    ),
  ];
}

async function openGrantDialog(actor: ReturnType<typeof userEvent.setup>) {
  await actor.click(
    await screen.findByRole("button", { name: "Grant access" }, LOAD),
  );
  return screen.findByRole("dialog", { name: "Grant access" });
}

describe("folder Access grants", () => {
  it("names every principal type, falls back for unknown ids and revokes a grant", async () => {
    const actor = userEvent.setup();
    const deleted: string[] = [];
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({
        grants: () =>
          page([
            grant({ id: "grant-1" }),
            grant({
              id: "grant-2",
              principal_type: "local_group",
              principal_id: localGroup.id,
              role: "team_editor",
              created_by: "local:someone-else",
            }),
            grant({
              id: "grant-3",
              principal_type: "service_account",
              principal_id: "sa:sa-1",
            }),
            grant({
              id: "grant-4",
              principal_type: "entra_group",
              principal_id: OID,
            }),
            grant({
              id: "grant-5",
              principal_type: "service_principal",
              principal_id: `entra:${TID}:${OID2}`,
            }),
            grant({
              id: "grant-6",
              principal_type: "user",
              principal_id: `entra:${TID}:${OID}`,
            }),
            grant({
              id: "grant-7",
              principal_type: "user",
              principal_id: "local:ghost",
            }),
          ]),
        extra: [
          http.delete("*/api/v1/identity/grants/:id", ({ params }) => {
            deleted.push(String(params.id));
            return new HttpResponse(null, { status: 204 });
          }),
        ],
      }),
    });
    const table = await screen.findByRole(
      "table",
      { name: "Grants at Ledger" },
      LOAD,
    );
    const rowOf = (text: string) => {
      const row = within(table).getByText(text, { exact: false }).closest("tr");
      if (!row) throw new Error(`no row for ${text}`);
      return row;
    };
    expect(
      await within(table).findAllByText("Alice Demo (demo-alice)"),
    ).not.toHaveLength(0);
    expect(rowOf("demo-ledger-editors")).toHaveTextContent("Local group");
    expect(rowOf("demo-ledger-editors")).toHaveTextContent("Team editor");
    // The granter is named when known, else shown as the short id with the full id as tooltip.
    expect(
      within(rowOf("demo-ledger-editors")).getByTitle("local:someone-else"),
    ).toHaveTextContent("local:someone-");
    expect(rowOf("demo-pipeline")).toHaveTextContent("Service account");
    expect(rowOf("Data Engineers")).toHaveTextContent("Entra group");
    expect(rowOf("Ingest App")).toHaveTextContent("Entra service principal");
    expect(rowOf("Erin Entra")).toHaveTextContent("Entra user");
    expect(
      within(table)
        .getAllByText(/Alice Demo/)[0]
        ?.closest("tr"),
    ).toHaveTextContent("Local user");
    expect(rowOf("local:ghost")).toHaveTextContent("Unknown principal");

    await actor.click(
      within(table).getByRole("button", {
        name: "Revoke Team reader from local:ghost",
      }),
    );
    let dialog = await screen.findByRole("alertdialog", {
      name: "Revoke Team reader from local:ghost at Ledger?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Keep grant" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(deleted).toEqual([]);

    await actor.click(
      within(table).getByRole("button", {
        name: "Revoke Team editor from demo-ledger-editors",
      }),
    );
    dialog = await screen.findByRole("alertdialog", {
      name: "Revoke Team editor from demo-ledger-editors at Ledger?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Revoke grant" }),
    );
    await waitFor(() => {
      expect(deleted).toEqual(["grant-2"]);
    });
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("sorts grants by principal, role and date", async () => {
    const actor = userEvent.setup();
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({
        grants: () =>
          page([
            grant({
              id: "grant-z",
              principal_id: "local:zz",
              role: "team_reader",
              created_at: "2026-11-01T10:00:00Z",
            }),
            grant({
              id: "grant-a",
              principal_id: "local:u-1",
              role: "team_editor",
              created_at: "2026-11-05T10:00:00Z",
            }),
          ]),
      }),
    });
    const table = await screen.findByRole(
      "table",
      { name: "Grants at Ledger" },
      LOAD,
    );
    await within(table).findAllByText("Alice Demo (demo-alice)");
    // Named principals sort by name, unknown ones by their raw id.
    const principals = await sortRowsBy(actor, table, "Principal");
    expect(principals[0]).toContain("Alice Demo");
    expect(principals[1]).toContain("local:zz");
    const roleCells = () =>
      within(table)
        .getAllByRole("row")
        .slice(1)
        .map((row) => within(row).getAllByRole("cell")[0]?.textContent);
    await actor.click(within(table).getByRole("button", { name: "Role" }));
    expect(roleCells()).toEqual(["Team editor", "Team reader"]);
    await actor.click(within(table).getByRole("button", { name: "Granted" }));
    expect(roleCells()).toEqual(["Team reader", "Team editor"]);
  });

  it("does not ask for user lists without user:read and ignores a failed list", async () => {
    const requested: string[] = [];
    renderApp("/folders/ledger-id/access", {
      folders,
      me: noUserRead,
      handlers: accessHandlers({
        grants: () =>
          page([
            grant({ id: "grant-1", principal_id: "local:u-1" }),
            grant({
              id: "grant-3",
              principal_type: "service_account",
              principal_id: "sa:sa-1",
            }),
          ]),
        extra: [
          http.get("*/api/v1/identity/users", () => {
            requested.push("users");
            return page([localUser]);
          }),
          http.get("*/api/v1/identity/entra/users", () => {
            requested.push("entra-users");
            return page([]);
          }),
          http.get("*/api/v1/identity/groups", () => problem(500, "internal")),
        ],
      }),
    });
    const table = await screen.findByRole(
      "table",
      { name: "Grants at Ledger" },
      LOAD,
    );
    expect(await within(table).findByText("demo-pipeline")).toBeVisible();
    // The local user is not resolvable without user:read.
    expect(within(table).getAllByText("Unknown principal")).toHaveLength(1);
    expect(requested).toEqual([]);
  });

  it("hides grant controls without grant:manage", async () => {
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({ perms: ["grant:read"] }),
    });
    const table = await screen.findByRole(
      "table",
      { name: "Grants at Ledger" },
      LOAD,
    );
    expect(
      await within(table).findAllByText("Alice Demo (demo-alice)"),
    ).not.toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Grant access" })).toBeNull();
    expect(within(table).queryByRole("button", { name: /Revoke/ })).toBeNull();
    expect(
      within(table).queryByRole("columnheader", { name: "Actions" }),
    ).toBeNull();
  });

  it("shows the empty state with one Grant access button", async () => {
    renderApp("/folders/ledger-id/access", {
      folders,
      handlers: accessHandlers({ grants: () => page([]) }),
    });
    expect(
      await screen.findByRole(
        "heading",
        { name: "No grants at this folder" },
        LOAD,
      ),
    ).toBeVisible();
    // The section header offers it; the empty state adds no second one.
    expect(
      screen.getAllByRole("button", { name: "Grant access" }),
    ).toHaveLength(1);
  });

  it("shows a grant load failure and retries", async () => {
    const actor = userEvent.setup();
    let fail = true;
    renderApp("/folders/ledger-id/access", {
      folders,
      handlers: accessHandlers({
        grants: () =>
          fail ? problem(500, "internal", "Grants unavailable.") : page([]),
      }),
    });
    expect(
      await screen.findByText("Grants unavailable.", {}, LOAD),
    ).toBeVisible();
    fail = false;
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", { name: "No grants at this folder" }),
    ).toBeVisible();
  });

  it("pages grants with the cursor", async () => {
    const actor = userEvent.setup();
    const searches: string[] = [];
    renderApp("/folders/ledger-id/access", {
      folders,
      handlers: accessHandlers({
        grants: () => page([]),
        extra: [
          http.get(
            "*/api/v1/identity/folders/ledger-id/grants",
            ({ request }) => {
              const url = new URL(request.url);
              searches.push(url.search);
              return url.searchParams.get("cursor") === "g2"
                ? page([grant({ id: "grant-9", principal_id: "local:late" })])
                : page(
                    [grant({ id: "grant-1", principal_id: "local:first" })],
                    "g2",
                  );
            },
          ),
        ],
      }),
    });
    expect(await screen.findByText("local:first", {}, LOAD)).toBeVisible();
    await actor.click(
      screen.getByRole("button", { name: "Load more results" }),
    );
    expect(await screen.findByText("local:late")).toBeVisible();
    expect(searches).toEqual(["?limit=100", "?limit=100&cursor=g2"]);
  });

  it("shows an effective-access failure in its card and retries", async () => {
    const actor = userEvent.setup();
    let fail = true;
    renderApp("/folders/ledger-id/access", {
      folders,
      handlers: accessHandlers({
        effective: () =>
          fail
            ? problem(500, "internal", "Access unavailable.")
            : HttpResponse.json({
                folder_id: "ledger-id",
                roles: [],
                permissions: [],
                inheritance_blocked: false,
                inheritance_blocked_at: null,
              }),
      }),
    });
    const message = await screen.findByText("Access unavailable.", {}, LOAD);
    const card = message.closest("section");
    if (!card) throw new Error("no card");
    expect(within(card).getByText("Your effective access")).toBeVisible();
    fail = false;
    await actor.click(
      within(card).getByRole("button", { name: "Retry loading" }),
    );
    await waitFor(() => {
      expect(within(card).queryByText("Access unavailable.")).toBeNull();
    });
  });
});

describe("grant dialog and principal picker", () => {
  it("requires a principal and a role before sending", async () => {
    const actor = userEvent.setup();
    let posted = false;
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({
        extra: [
          http.post("*/api/v1/identity/folders/:id/grants", () => {
            posted = true;
            return HttpResponse.json({}, { status: 201 });
          }),
        ],
      }),
    });
    const dialog = await openGrantDialog(actor);
    expect(
      within(dialog).getByText(
        "The role applies at acme/ledger and every folder below it.",
      ),
    ).toBeVisible();
    await actor.click(
      within(dialog).getByRole("button", { name: "Grant access" }),
    );
    expect(within(dialog).getByText("Choose who gets access.")).toBeVisible();
    expect(within(dialog).getByText("Choose a role.")).toBeVisible();
    expect(posted).toBe(false);
    await actor.selectOptions(
      within(dialog).getByLabelText("Role"),
      "team_reader",
    );
    expect(
      within(dialog).getByText("Reads this team's contracts."),
    ).toBeVisible();
  });

  it("asks for nothing until two characters are typed, then matches name, username, email and object ID", async () => {
    const actor = userEvent.setup();
    const { queryClient } = renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({
        extra: [
          http.get("*/api/v1/identity/users", () => {
            return page([
              { ...localUser, email: "alice@corp.test" },
              {
                ...localUser,
                id: "u-2",
                username: "demo-old",
                display_name: "Old Timer",
                is_active: false,
              },
            ]);
          }),
          http.get("*/api/v1/identity/entra/users", () => {
            return page([
              entraUser(),
              entraUser({
                id: "eu-2",
                display_name: "No Upn",
                preferred_username: null,
                object_id: OID2,
                principal_id: `entra:${TID}:${OID2}`,
              }),
            ]);
          }),
        ],
      }),
    });
    const dialog = await openGrantDialog(actor);
    // Search-only: a hint, no list, no request.
    expect(
      within(dialog).getByText(
        "Type at least 2 characters: name, username, email or object ID",
      ),
    ).toBeVisible();
    expect(within(dialog).queryByRole("listbox")).toBeNull();
    const search = within(dialog).getByLabelText("Search users");
    await actor.type(search, "d");
    expect(within(dialog).queryByRole("listbox")).toBeNull();
    // The picker's own lists are not read before the search starts.
    const pickerKey = ["identity", "picker", "user", "ledger-id"];
    expect(queryClient.getQueryData(pickerKey)).toBeUndefined();

    // Username (and name): both local users, the deactivated one disabled.
    await actor.type(search, "emo");
    await within(dialog).findByRole("listbox", { name: "User results" });
    expect(queryClient.getQueryData(pickerKey)).toBeDefined();
    // The list is replaced by the hint while the box is empty, so look it up each time.
    const list = () =>
      within(dialog).getByRole("listbox", { name: "User results" });
    expect(
      within(list()).getByRole("option", { name: /Alice Demo/ }),
    ).not.toHaveAttribute("aria-disabled");
    expect(within(list()).getByText("demo-alice · Local")).toBeVisible();
    expect(
      within(list()).getByRole("option", { name: /Old Timer \(deactivated\)/ }),
    ).toHaveAttribute("aria-disabled", "true");
    expect(
      within(list()).queryByRole("option", { name: /Erin Entra/ }),
    ).toBeNull();
    // Email.
    await actor.clear(search);
    await actor.type(search, "corp.test");
    expect(
      within(list()).getByRole("option", { name: /Alice Demo/ }),
    ).toBeVisible();
    expect(
      within(list()).queryByRole("option", { name: /Old Timer/ }),
    ).toBeNull();
    // Entra preferred username and object ID.
    await actor.clear(search);
    await actor.type(search, "erin@");
    expect(
      within(list()).getByRole("option", { name: /Erin Entra/ }),
    ).toBeVisible();
    expect(within(list()).getByText("erin@example.com · Entra")).toBeVisible();
    expect(
      within(list()).queryByRole("option", { name: /Alice Demo/ }),
    ).toBeNull();
    await actor.clear(search);
    await actor.type(search, "9a8b7c6d");
    expect(within(list()).getByText(`${OID2} · Entra`)).toBeVisible();
    expect(
      within(list()).queryByRole("option", { name: /Erin Entra/ }),
    ).toBeNull();
    // A pasted full object ID shows exactly the matching item.
    await actor.clear(search);
    await actor.type(search, OID.toUpperCase());
    expect(within(list()).getAllByRole("option")).toHaveLength(1);
    expect(
      within(list()).getByRole("option", { name: /Erin Entra/ }),
    ).toBeVisible();
    await actor.clear(search);
    await actor.type(search, "zzz");
    expect(within(dialog).getByText("No users match.")).toBeVisible();
  });

  it("shows at most 20 matches and asks for more characters", async () => {
    const actor = userEvent.setup();
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({
        extra: [
          http.get("*/api/v1/identity/users", () =>
            page(
              Array.from({ length: 25 }, (_, i) => ({
                ...localUser,
                id: `u-${String(i)}`,
                username: `batch-${String(i)}`,
                display_name: `Batch user ${String(i)}`,
              })),
            ),
          ),
        ],
      }),
    });
    const dialog = await openGrantDialog(actor);
    await actor.type(within(dialog).getByLabelText("Search users"), "batch");
    const list = await within(dialog).findByRole("listbox", {
      name: "User results",
    });
    expect(within(list).getAllByRole("option")).toHaveLength(20);
    expect(
      within(dialog).getByText(
        "Showing 20 of 25 matches. Type more to narrow the list.",
      ),
    ).toBeVisible();
    await actor.type(within(dialog).getByLabelText("Search users"), "-24");
    expect(within(list).getAllByRole("option")).toHaveLength(1);
    expect(within(dialog).queryByText(/Type more to narrow/)).toBeNull();
  });

  it("has no free-text username or group object ID field", async () => {
    const actor = userEvent.setup();
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers(),
    });
    const dialog = await openGrantDialog(actor);
    expect(within(dialog).queryByLabelText(/Or enter/)).toBeNull();
    await actor.selectOptions(
      within(dialog).getByLabelText("Principal type"),
      "entra_group",
    );
    expect(within(dialog).queryByLabelText(/Or enter/)).toBeNull();
    expect(within(dialog).queryByLabelText(/object ID$/)).toBeNull();
  });

  it("notes a partial directory and closes on Go back", async () => {
    const actor = userEvent.setup();
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({
        extra: [
          http.get("*/api/v1/identity/users", () => page([localUser])),
          http.get("*/api/v1/identity/entra/users", () =>
            problem(503, "entra-unavailable"),
          ),
        ],
      }),
    });
    const dialog = await openGrantDialog(actor);
    await actor.type(within(dialog).getByLabelText("Search users"), "alice");
    expect(
      await within(dialog).findByRole("option", { name: /Alice Demo/ }),
    ).toBeVisible();
    expect(
      within(dialog).getByText("Some directories could not be listed."),
    ).toBeVisible();
    await actor.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Grant access" })).toBeNull();
    });
  });

  it("warns when every user directory fails", async () => {
    const actor = userEvent.setup();
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({
        extra: [
          http.get("*/api/v1/identity/users", () => problem(500, "internal")),
          http.get("*/api/v1/identity/entra/users", () =>
            problem(500, "internal"),
          ),
        ],
      }),
    });
    const dialog = await openGrantDialog(actor);
    await actor.type(within(dialog).getByLabelText("Search users"), "alice");
    expect(
      await within(dialog).findByText("The user list could not be loaded."),
    ).toBeVisible();
  });

  it("offers grantable local groups, service accounts and service principals", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({
        extra: [
          http.get("*/api/v1/identity/service-accounts", () =>
            page([
              serviceAccount,
              {
                ...serviceAccount,
                id: "sa-2",
                principal_id: "sa:sa-2",
                name: "retired-bot",
                is_active: false,
              },
            ]),
          ),
          http.get("*/api/v1/identity/entra/service-principals", () =>
            page([
              entraSp(),
              entraSp({
                id: "sp-2",
                principal_id: "entra:x:y",
                object_id: "dead-oid",
                display_name: "Dead App",
                is_active: false,
              }),
            ]),
          ),
          http.post(
            "*/api/v1/identity/folders/ledger-id/grants",
            async ({ request }) => {
              bodies.push(await request.json());
              return HttpResponse.json({}, { status: 201 });
            },
          ),
        ],
      }),
    });
    const dialog = await openGrantDialog(actor);
    const type = within(dialog).getByLabelText("Principal type");
    expect(
      within(type)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual([
      "User",
      "Local group",
      "Entra group",
      "Service account",
      "Entra service principal",
    ]);
    await actor.selectOptions(type, "local_group");
    await actor.type(
      within(dialog).getByLabelText("Search local groups"),
      "ledger",
    );
    expect(
      await within(dialog).findByRole("option", {
        name: /demo-ledger-editors/,
      }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("listbox", { name: "Local group results" }),
    ).toBeVisible();

    await actor.selectOptions(type, "service_account");
    await actor.type(
      within(dialog).getByLabelText("Search service accounts"),
      "bot",
    );
    expect(
      await within(dialog).findByRole("option", {
        name: /retired-bot \(deactivated\)/,
      }),
    ).toHaveAttribute("aria-disabled", "true");
    await actor.clear(within(dialog).getByLabelText("Search service accounts"));
    await actor.type(
      within(dialog).getByLabelText("Search service accounts"),
      "pipeline",
    );
    expect(
      await within(dialog).findByRole("option", { name: /demo-pipeline/ }),
    ).not.toHaveAttribute("aria-disabled");

    await actor.selectOptions(type, "service_principal");
    await actor.type(
      within(dialog).getByLabelText("Search Entra service principals"),
      "app",
    );
    expect(
      await within(dialog).findByRole("option", { name: /Ingest App/ }),
    ).not.toHaveAttribute("aria-disabled");
    expect(within(dialog).getByText(OID2)).toBeVisible();
    expect(
      within(dialog).getByRole("option", { name: /Dead App \(deactivated\)/ }),
    ).toHaveAttribute("aria-disabled", "true");
    await actor.click(
      within(dialog).getByRole("option", { name: /Ingest App/ }),
    );
    await actor.selectOptions(
      within(dialog).getByLabelText("Role"),
      "team_editor",
    );
    expect(
      within(dialog).getByText(/Grant/, { selector: "p" }),
    ).toHaveTextContent(
      "Grant Team editor to Ingest App (entra service principal) at acme/ledger",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Grant access" }),
    );
    await waitFor(() => {
      expect(bodies).toEqual([
        {
          principal_type: "service_principal",
          principal_id: `entra:${TID}:${OID2}`,
          role: "team_editor",
        },
      ]);
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Grant access" })).toBeNull();
    });
  });

  it("a pasted object id finds the registered Entra group", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({
        extra: [
          http.get("*/api/v1/identity/entra/groups", () =>
            page([
              entraGroup(),
              entraGroup({
                id: "eg-2",
                object_id: OID2,
                display_name: "Other Group",
              }),
            ]),
          ),
          http.post(
            "*/api/v1/identity/folders/ledger-id/grants",
            async ({ request }) => {
              bodies.push(await request.json());
              return problem(500, "internal");
            },
          ),
        ],
      }),
    });
    const dialog = await openGrantDialog(actor);
    await actor.selectOptions(
      within(dialog).getByLabelText("Principal type"),
      "entra_group",
    );
    const search = within(dialog).getByLabelText("Search Entra groups");
    await actor.type(search, OID.toUpperCase());
    const list = await within(dialog).findByRole("listbox", {
      name: "Entra group results",
    });
    // Exactly the matching item, picked by its registered record.
    expect(within(list).getAllByRole("option")).toHaveLength(1);
    const registered = within(list).getByRole("option", {
      name: /Data Engineers/,
    });
    await actor.click(registered);
    // The list closes and the box holds the group's name.
    expect(within(dialog).queryByRole("listbox")).toBeNull();
    expect(search).toHaveValue("Data Engineers");
    await actor.selectOptions(
      within(dialog).getByLabelText("Role"),
      "team_reader",
    );
    expect(
      within(dialog).getByText(/Grant/, { selector: "p" }),
    ).toHaveTextContent(
      "Grant Team reader to Data Engineers (entra group) at acme/ledger",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Grant access" }),
    );
    await waitFor(() => {
      expect(bodies).toEqual([
        {
          principal_type: "entra_group",
          principal_id: OID,
          role: "team_reader",
        },
      ]);
    });
    // A refusal without a detail renders the ProblemAlert, not the detail alert.
    expect(await within(dialog).findByText("Refused 500")).toBeVisible();
    expect(within(dialog).queryByText("The grant was refused")).toBeNull();
    // A partial name finds it as well.
    await actor.clear(search);
    await actor.type(search, "data");
    const again = await within(dialog).findByRole("listbox", {
      name: "Entra group results",
    });
    expect(
      within(again).getByRole("option", { name: /Data Engineers/ }),
    ).toBeVisible();
    expect(
      within(again).queryByRole("option", { name: /Other Group/ }),
    ).toBeNull();
  });

  it("shows a refusal's detail with and without a slug, and resets on Escape", async () => {
    const actor = userEvent.setup();
    const replies = [
      () => problem(403, "grant-ceiling-exceeded", "Above your own role."),
      () =>
        HttpResponse.json(
          { title: "Refused", status: 409, detail: "Already granted." },
          {
            status: 409,
            headers: { "Content-Type": "application/problem+json" },
          },
        ),
    ];
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({
        extra: [
          http.post("*/api/v1/identity/folders/ledger-id/grants", () => {
            const reply = replies.shift();
            if (!reply) throw new Error("unexpected POST");
            return reply();
          }),
        ],
      }),
    });
    let dialog = await openGrantDialog(actor);
    await actor.type(within(dialog).getByLabelText("Search users"), "alice");
    await actor.click(
      await within(dialog).findByRole("option", { name: /Alice Demo/ }),
    );
    await actor.selectOptions(
      within(dialog).getByLabelText("Role"),
      "team_reader",
    );
    const submit = within(dialog).getByRole("button", { name: "Grant access" });
    await actor.click(submit);
    expect(
      await within(dialog).findByText("The grant was refused"),
    ).toBeVisible();
    expect(within(dialog).getByText("Above your own role.")).toBeVisible();
    expect(within(dialog).getByText("grant-ceiling-exceeded")).toBeVisible();
    await actor.click(submit);
    expect(await within(dialog).findByText("Already granted.")).toBeVisible();
    expect(within(dialog).queryByText("grant-ceiling-exceeded")).toBeNull();

    await actor.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Grant access" })).toBeNull();
    });
    dialog = await openGrantDialog(actor);
    expect(within(dialog).queryByText("Already granted.")).toBeNull();
    expect(within(dialog).getByLabelText("Role")).toHaveValue("");
  });

  it("says no role can be granted and disables the grant", async () => {
    const actor = userEvent.setup();
    renderApp("/folders/ledger-id/access", {
      folders,
      me: reader(),
      handlers: accessHandlers({ grantable: [] }),
    });
    const dialog = await openGrantDialog(actor);
    expect(
      within(dialog).getByText("You cannot grant any role at this folder."),
    ).toBeVisible();
    expect(within(dialog).queryByLabelText("Role")).toBeNull();
    expect(
      within(dialog).getByRole("button", { name: "Grant access" }),
    ).toBeDisabled();
  });

  it("describes the root folder by name", async () => {
    const actor = userEvent.setup();
    renderApp(`/folders/${ROOT_ID}/access`, {
      folders,
      me: reader(),
      handlers: accessHandlers(),
    });
    const dialog = await openGrantDialog(actor);
    expect(
      within(dialog).getByText(
        "The role applies at Root and every folder below it.",
      ),
    ).toBeVisible();
    // The root holds no inheritance switch.
    expect(screen.queryByRole("heading", { name: "Inheritance" })).toBeNull();
  });
});

describe("block inheritance", () => {
  it("blocks inheritance at a team with parent folder:set-block and keeps the folder", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    const { router } = renderApp("/folders/ledger-id/access", {
      folders,
      handlers: accessHandlers({
        parentPerms: ["folder:set-block"],
        extra: [
          http.put(
            "*/api/v1/identity/folders/ledger-id/block-inheritance",
            async ({ request }) => {
              bodies.push(await request.json());
              return HttpResponse.json({ ...ledger, block_inheritance: true });
            },
          ),
        ],
      }),
    });
    expect(
      await screen.findByRole("heading", { name: "Inheritance" }, LOAD),
    ).toBeVisible();
    expect(screen.getByText("Inherits from above")).toBeVisible();
    await actor.click(
      await screen.findByRole("button", { name: "Block inheritance" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Block inheritance at Ledger?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Block inheritance" }),
    );
    await waitFor(() => {
      expect(bodies).toEqual([{ enabled: true }]);
    });
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(router.state.location.pathname).toBe("/folders/ledger-id/access");
  });

  it("allows inheritance again and goes to the parent when access is lost", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    const blocked = { ...ledger, block_inheritance: true };
    const { router } = renderApp("/folders/ledger-id/access", {
      folders: [rootFolder, acme, blocked, payments],
      handlers: accessHandlers({
        parentPerms: ["folder:set-block"],
        extra: [
          http.put(
            "*/api/v1/identity/folders/ledger-id/block-inheritance",
            async ({ request }) => {
              bodies.push(await request.json());
              return HttpResponse.json({
                id: "ledger-id",
                block_inheritance: false,
              });
            },
          ),
        ],
      }),
    });
    expect(
      await screen.findByText("Inheritance blocked", {}, LOAD),
    ).toBeVisible();
    await actor.click(
      await screen.findByRole("button", { name: "Allow inheritance" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Allow inheritance at Ledger?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Allow inheritance" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders/acme-id");
    });
    expect(bodies).toEqual([{ enabled: false }]);
  });

  it("hides the switch without parent folder:set-block and for projects", async () => {
    renderApp("/folders/ledger-id/access", {
      folders,
      handlers: accessHandlers({ parentPerms: [] }),
    });
    expect(
      await screen.findByRole("heading", { name: "Inheritance" }, LOAD),
    ).toBeVisible();
    await screen.findByRole("table", { name: "Grants at Ledger" });
    expect(screen.queryByRole("button", { name: /inheritance/ })).toBeNull();
  });

  it("shows no inheritance card on a project", async () => {
    renderApp("/folders/payments-id/access", {
      folders,
      handlers: accessHandlers({ parentPerms: ["folder:set-block"] }),
    });
    expect(
      await screen.findByRole(
        "heading",
        { name: "Grants at this folder" },
        LOAD,
      ),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Inheritance" })).toBeNull();
  });
});
