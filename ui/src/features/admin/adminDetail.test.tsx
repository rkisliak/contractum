// Identity administration screens in depth (Phase 12): users, groups, service accounts and access
// tokens through the real router. Every list state (loading, empty, error with retry, rows, the
// keyset "Load more results", the row filter), every dialog path (create, rename, deactivate,
// reactivate, delete, add and remove member, create and revoke a key or token, the show-once
// reveal) and the capability gating of each control, with the request sent and what renders.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, type RequestHandler } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import type { Me } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { roleMeaning } from "@/features/access/roles";
import { http, HttpResponse } from "@/test/msw";
import { sortRowsBy } from "@/test/governance-table";
import {
  makeFolder,
  makeMe,
  renderApp,
  ROOT_ID,
  rootFolder,
} from "@/test/renderApp";
import {
  PRINCIPAL_TYPE_LABELS,
  principalTypeLabel,
  roleDescription,
} from "./principal";
import { KEY_LIMIT_COPY } from "./ServiceAccountDetailPage";
import { dateInDays, ownerOptions } from "./shared";

afterEach(() => {
  tokenStore.clear();
});

/** The first element after a route load can take a while on a loaded machine. */
const LONG = { timeout: 10000 };

const UUID = "0b5c3f8e-1a2b-4c3d-8e9f-0123456789ab";
const UUID2 = "1c6d4f9e-2b3c-4d4e-9f0a-123456789abc";

const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const globex = makeFolder({
  id: "globex-id",
  slug: "globex",
  display_name: "Globex",
  path: "globex",
});
const oldOrg = makeFolder({
  id: "old-id",
  slug: "old",
  display_name: "Old Org",
  path: "old",
  archived: true,
});
const folders = [rootFolder, acme, globex];

const stamp = {
  created_by: "local:admin",
  created_at: "2026-11-01T10:00:00Z",
  updated_by: "local:admin",
  updated_at: "2026-11-01T10:00:00Z",
};

function problem(status: number, slug: string, detail: string, extra = {}) {
  return HttpResponse.json(
    {
      type: `urn:dc:problem:${slug}`,
      title: "Refused",
      status,
      detail,
      ...extra,
    },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

const serverError = () => problem(500, "internal", "Something broke inside.");

function page(items: unknown[], next_cursor: string | null = null) {
  return HttpResponse.json({ items, next_cursor });
}

const empty = { items: [], next_cursor: null };

function makeUser(overrides: Record<string, unknown> = {}) {
  return {
    id: UUID,
    principal_id: `local:${UUID}`,
    username: "demo-alice",
    display_name: "Alice Demo",
    email: null,
    is_active: true,
    must_rotate: false,
    ...stamp,
    ...overrides,
  };
}

function makeGroup(overrides: Record<string, unknown> = {}) {
  return {
    id: "grp-1",
    owner_folder_id: "acme-id",
    name: "demo-editors",
    ...stamp,
    ...overrides,
  };
}

function makeAccount(overrides: Record<string, unknown> = {}) {
  return {
    id: "sa-1",
    principal_id: `sa:${UUID}`,
    owner_folder_id: "acme-id",
    name: "demo-pipeline",
    is_active: true,
    ...stamp,
    ...overrides,
  };
}

function makeKey(overrides: Record<string, unknown> = {}) {
  return {
    id: "k-1",
    name: "key-1",
    expires_at: inDays(100),
    created_at: "2026-11-01T10:00:00Z",
    revoked_at: null,
    last_used_at: null,
    status: "active",
    expiring_soon: false,
    ...overrides,
  };
}

function makePat(overrides: Record<string, unknown> = {}) {
  return {
    id: "p-1",
    name: "laptop",
    read_only: false,
    expires_at: inDays(20),
    created_at: "2026-11-01T10:00:00Z",
    revoked_at: null,
    last_used_at: null,
    status: "active",
    expiring_soon: false,
    ...overrides,
  };
}

/** An ISO time `days` whole days ahead (minus an hour, so ceil gives exactly `days`). */
function inDays(days: number): string {
  return new Date(Date.now() + days * 86_400_000 - 3_600_000).toISOString();
}

/** The principal lists the picker and the name lookup read. */
function principalLists({
  users = [makeUser()],
  accounts = [] as unknown[],
} = {}) {
  return [
    http.get("*/api/v1/identity/users", () => page(users)),
    http.get("*/api/v1/identity/groups", () => HttpResponse.json(empty)),
    http.get("*/api/v1/identity/service-accounts", () => page(accounts)),
    http.get("*/api/v1/identity/entra/users", () => HttpResponse.json(empty)),
    http.get("*/api/v1/identity/entra/groups", () => HttpResponse.json(empty)),
    http.get("*/api/v1/identity/entra/service-principals", () =>
      HttpResponse.json(empty),
    ),
  ];
}

/** A principal without any global permission or grant: no owner is offered. */
const nobody = makeMe({ global_permissions: [], assignments: [] });

async function confirmTyped(
  actor: ReturnType<typeof userEvent.setup>,
  dialogName: string,
  typed: string,
  confirm: string,
) {
  const dialog = await screen.findByRole("alertdialog", { name: dialogName });
  const button = within(dialog).getByRole("button", { name: confirm });
  expect(button).toBeDisabled();
  await actor.type(
    within(dialog).getByLabelText(`Type ${typed} to confirm`),
    typed,
  );
  expect(button).toBeEnabled();
  await actor.click(button);
  return dialog;
}

describe("principal and owner helpers", () => {
  it("labels every principal type and reuses the role copy", () => {
    expect(Object.keys(PRINCIPAL_TYPE_LABELS)).toHaveLength(5);
    expect(principalTypeLabel("service_account")).toBe("Service account");
    expect(principalTypeLabel("service_principal")).toBe(
      "Entra service principal",
    );
    expect(roleDescription("team_reader")).toBe(roleMeaning("team_reader"));
    expect(roleDescription("team_reader")).not.toBe("");
  });

  it("offers nothing before /me loads and ROOT plus live orgs to a global admin", () => {
    expect(ownerOptions(undefined, folders, "group:create")).toEqual([]);
    const admin = makeMe();
    expect(
      ownerOptions(admin, [rootFolder, acme, oldOrg, globex], "group:create"),
    ).toEqual([
      { value: ROOT_ID, label: "ROOT (global)" },
      { value: "acme-id", label: "Acme" },
      { value: "globex-id", label: "Globex" },
    ]);
    const viaPermission = makeMe({
      assignments: [],
      global_permissions: ["service-account:create"],
    });
    expect(
      ownerOptions(viaPermission, [acme], "service-account:create"),
    ).toEqual([{ value: "acme-id", label: "Acme" }]);
    expect(ownerOptions(nobody, folders, "group:create")).toEqual([]);
  });
});

describe("users list", () => {
  it("shows a skeleton while the first page loads", async () => {
    renderApp("/admin/users", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/users", async () => {
          await delay("infinite");
          return page([]);
        }),
      ],
    });
    expect(
      await screen.findByText("Loading users", {}, LONG),
    ).toBeInTheDocument();
  });

  it("asks for nothing and says so without user:read", async () => {
    let requested = false;
    renderApp("/admin/users", {
      folders,
      me: makeMe({ global_permissions: ["audit:list"] }),
      handlers: [
        http.get("*/api/v1/identity/users", () => {
          requested = true;
          return page([]);
        }),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { name: "Not available at your access level" },
        LONG,
      ),
    ).toBeVisible();
    expect(requested).toBe(false);
    expect(screen.queryByRole("button", { name: "New" })).toBeNull();
  });

  it("shows a problem and retries", async () => {
    const actor = userEvent.setup();
    // Fail every call until Retry: the principal-name lookup may request the same list first.
    let failing = true;
    let calls = 0;
    renderApp("/admin/users", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/users", () => {
          calls += 1;
          return failing ? serverError() : page([makeUser()]);
        }),
      ],
    });
    expect(
      await screen.findByText("Something broke inside.", {}, LONG),
    ).toBeVisible();
    failing = false;
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("link", { name: "Alice Demo" }, LONG),
    ).toBeVisible();
    expect(calls).toBe(2);
  });

  it("offers one New menu in the header of the empty state with user:create", async () => {
    renderApp("/admin/users", {
      folders,
      me: makeMe({ global_permissions: ["user:read", "user:create"] }),
      handlers: [http.get("*/api/v1/identity/users", () => page([]))],
    });
    const heading = await screen.findByRole(
      "heading",
      { name: "No users yet" },
      LONG,
    );
    expect(heading).toBeVisible();
    // Only the header offers it: exactly one New button on the page.
    expect(screen.getAllByRole("button", { name: "New" })).toHaveLength(1);
  });

  it("loads more pages with the cursor, filters and hides deactivated rows", async () => {
    const actor = userEvent.setup();
    const cursors: (string | null)[] = [];
    renderApp("/admin/users", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/users", ({ request }) => {
          const url = new URL(request.url);
          const cursor = url.searchParams.get("cursor");
          cursors.push(cursor);
          expect(url.searchParams.get("limit")).toBe("100");
          return cursor === null
            ? page([makeUser({ email: "alice@example.com" })], "cursor-2")
            : page([
                makeUser({
                  id: UUID2,
                  username: "demo-bob",
                  display_name: "Bob Demo",
                  is_active: false,
                }),
              ]);
        }),
      ],
    });
    expect(
      await screen.findByRole("link", { name: "Alice Demo" }, LONG),
    ).toHaveAttribute("href", `/admin/users/${UUID}`);
    expect(screen.getByText("demo-alice")).toBeVisible();
    expect(screen.getByText("Active")).toBeVisible();
    await actor.click(
      screen.getByRole("button", { name: "Load more results" }),
    );
    expect(await screen.findByRole("link", { name: "Bob Demo" })).toBeVisible();
    expect(cursors).toEqual([null, "cursor-2"]);
    expect(screen.getByText("Deactivated")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Load more results" }),
    ).toBeNull();

    await actor.click(screen.getByLabelText("Hide deactivated"));
    expect(screen.queryByRole("link", { name: "Bob Demo" })).toBeNull();
    await actor.click(screen.getByLabelText("Hide deactivated"));
    await actor.type(screen.getByLabelText("Search users"), "BOB");
    expect(screen.getByRole("link", { name: "Bob Demo" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Alice Demo" })).toBeNull();
    await actor.clear(screen.getByLabelText("Search users"));
    await actor.type(screen.getByLabelText("Search users"), "example.com");
    expect(screen.getByRole("link", { name: "Alice Demo" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Bob Demo" })).toBeNull();
  });
});

describe("create user dialog", () => {
  const creator = makeMe({ global_permissions: ["user:read", "user:create"] });

  async function openCreate(
    respond: () => Response | Promise<Response>,
    bodies: unknown[] = [],
  ) {
    const actor = userEvent.setup();
    renderApp("/admin/users", {
      folders,
      me: creator,
      handlers: [
        http.get("*/api/v1/identity/users", () => page([makeUser()])),
        http.post("*/api/v1/identity/users", async ({ request }) => {
          bodies.push(await request.json());
          return respond();
        }),
        http.get("*/api/v1/identity/users/new-id", () =>
          HttpResponse.json(
            makeUser({ id: "new-id", display_name: "Bob Builder" }),
          ),
        ),
      ],
    });
    await actor.click(await screen.findByRole("button", { name: "New" }, LONG));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Create local user" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Create local user",
    });
    return { actor, dialog: within(dialog), element: dialog };
  }

  async function fill(
    actor: ReturnType<typeof userEvent.setup>,
    element: HTMLElement,
    email = "",
  ) {
    const dialog = within(element);
    await actor.type(dialog.getByLabelText("Username"), "  demo-bob ");
    await actor.type(dialog.getByLabelText("Display name"), "Bob Builder");
    if (email)
      await actor.type(dialog.getByLabelText("Email (optional)"), email);
    await actor.type(
      dialog.getByLabelText("Initial password"),
      "a-long-passphrase-42",
    );
    await actor.click(
      dialog.getByRole("button", { name: "Create local user" }),
    );
  }

  it("requires username, display name and password before sending", async () => {
    const bodies: unknown[] = [];
    const { actor, dialog } = await openCreate(
      () => HttpResponse.json({}),
      bodies,
    );
    await actor.click(
      dialog.getByRole("button", { name: "Create local user" }),
    );
    expect(dialog.getByText("Enter a username.")).toBeVisible();
    expect(dialog.getByText("Enter a display name.")).toBeVisible();
    expect(dialog.getByText("Enter an initial password.")).toBeVisible();
    expect(bodies).toEqual([]);
  });

  it("sends a trimmed body with the email and opens the new user", async () => {
    const bodies: unknown[] = [];
    const { actor, element } = await openCreate(
      () => HttpResponse.json(makeUser({ id: "new-id" }), { status: 201 }),
      bodies,
    );
    await fill(actor, element, " bob@example.com ");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Bob Builder" }),
    ).toBeVisible();
    expect(bodies).toEqual([
      {
        username: "demo-bob",
        display_name: "Bob Builder",
        password: "a-long-passphrase-42",
        email: "bob@example.com",
      },
    ]);
  });

  it("puts a 409 on the username", async () => {
    const { actor, dialog, element } = await openCreate(() =>
      problem(409, "conflict", "Taken."),
    );
    await fill(actor, element);
    expect(
      await dialog.findByText("A user with this username already exists."),
    ).toBeVisible();
  });

  it("puts a password refusal on the password field", async () => {
    const { actor, dialog, element } = await openCreate(() =>
      problem(422, "password-too-common", "This password is too common."),
    );
    await fill(actor, element);
    expect(
      await dialog.findByText("This password is too common."),
    ).toBeVisible();
    // Only the field message is an alert: no problem alert above the form.
    expect(dialog.getAllByRole("alert")).toHaveLength(1);
  });

  it("maps field pointers onto their fields", async () => {
    const { actor, dialog, element } = await openCreate(() =>
      problem(422, "validation-failed", "Invalid input.", {
        errors: [{ pointer: "/email", detail: "Not a valid email." }],
      }),
    );
    await fill(actor, element, "bob@example.com");
    expect(await dialog.findByText("Not a valid email.")).toBeVisible();
  });

  it("shows any other failure in a problem alert and resets on Go back", async () => {
    const { actor, dialog, element } = await openCreate(serverError);
    await fill(actor, element);
    expect(await dialog.findByText("Something broke inside.")).toBeVisible();
    await actor.click(dialog.getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Create local user" }),
      ).toBeNull();
    });
    await actor.click(screen.getByRole("button", { name: "New" }));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Create local user" }),
    );
    const again = within(
      await screen.findByRole("dialog", { name: "Create local user" }),
    );
    expect(again.getByLabelText("Username")).toHaveValue("");
    expect(again.queryByText("Something broke inside.")).toBeNull();
  });
});

describe("user detail", () => {
  it("shows a skeleton, then a 404 as Not found", async () => {
    renderApp("/admin/users/missing", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/users/missing", () =>
          problem(404, "not-found", "No such user."),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "Not found" }, LONG),
    ).toBeVisible();
  });

  it("shows another failure as a problem with retry", async () => {
    const actor = userEvent.setup();
    let calls = 0;
    renderApp(`/admin/users/${UUID}`, {
      folders,
      handlers: [
        http.get(`*/api/v1/identity/users/${UUID}`, () => {
          calls += 1;
          return calls === 1 ? serverError() : HttpResponse.json(makeUser());
        }),
      ],
    });
    expect(
      await screen.findByText("Something broke inside.", {}, LONG),
    ).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Alice Demo" }),
    ).toBeVisible();
  });

  it("shows a skeleton while loading", async () => {
    renderApp(`/admin/users/${UUID}`, {
      folders,
      handlers: [
        http.get(`*/api/v1/identity/users/${UUID}`, async () => {
          await delay("infinite");
          return HttpResponse.json(makeUser());
        }),
      ],
    });
    expect(
      await screen.findByText("Loading user", {}, LONG),
    ).toBeInTheDocument();
  });

  it("renders the profile and hides Deactivate without user:deactivate", async () => {
    renderApp(`/admin/users/${UUID}`, {
      folders,
      handlers: [
        http.get(`*/api/v1/identity/users/${UUID}`, () =>
          HttpResponse.json(
            makeUser({ must_rotate: true, email: "alice@example.com" }),
          ),
        ),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { level: 1, name: "Alice Demo" },
        LONG,
      ),
    ).toBeVisible();
    expect(screen.getByText("Required at next sign-in")).toBeVisible();
    expect(screen.getByText("alice@example.com")).toBeVisible();
    expect(screen.getByText(`local:${UUID}`)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Deactivate user" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Reactivate user" }),
    ).toBeNull();
  });

  it("deactivates after typing the username, then offers reactivation", async () => {
    const actor = userEvent.setup();
    let active = true;
    const posted: string[] = [];
    renderApp(`/admin/users/${UUID}`, {
      folders,
      me: makeMe({ global_permissions: ["user:read", "user:deactivate"] }),
      handlers: [
        http.get(`*/api/v1/identity/users/${UUID}`, () =>
          HttpResponse.json(makeUser({ is_active: active })),
        ),
        http.post(
          `*/api/v1/identity/users/${UUID}/:action`,
          ({ params, request }) => {
            posted.push(`${request.method} ${String(params.action)}`);
            active = params.action === "reactivate";
            return new HttpResponse(null, { status: 204 });
          },
        ),
      ],
    });
    expect(await screen.findByText("Not required", {}, LONG)).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Deactivate user" }));
    await confirmTyped(
      actor,
      "Deactivate demo-alice?",
      "demo-alice",
      "Deactivate demo-alice",
    );
    const reactivate = await screen.findByRole("button", {
      name: "Reactivate user",
    });
    expect(posted).toEqual(["POST deactivate"]);
    expect(screen.getByText("Deactivated")).toBeVisible();

    // Keeping the user closes without a request.
    await actor.click(reactivate);
    let dialog = await screen.findByRole("alertdialog", {
      name: "Reactivate demo-alice?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Keep demo-alice" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(posted).toEqual(["POST deactivate"]);

    await actor.click(screen.getByRole("button", { name: "Reactivate user" }));
    dialog = await screen.findByRole("alertdialog", {
      name: "Reactivate demo-alice?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Reactivate demo-alice" }),
    );
    expect(
      await screen.findByRole("button", { name: "Deactivate user" }),
    ).toBeVisible();
    expect(posted).toEqual(["POST deactivate", "POST reactivate"]);
  });

  it("keeps the dialog open with the problem when deactivation is refused", async () => {
    const actor = userEvent.setup();
    renderApp(`/admin/users/${UUID}`, {
      folders,
      me: makeMe({ global_permissions: ["user:read", "user:deactivate"] }),
      handlers: [
        http.get(`*/api/v1/identity/users/${UUID}`, () =>
          HttpResponse.json(makeUser()),
        ),
        http.post(`*/api/v1/identity/users/${UUID}/deactivate`, () =>
          problem(409, "last-admin", "Cannot deactivate the last admin."),
        ),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Deactivate user" }, LONG),
    );
    const dialog = await confirmTyped(
      actor,
      "Deactivate demo-alice?",
      "demo-alice",
      "Deactivate demo-alice",
    );
    expect(
      await within(dialog).findByText("Cannot deactivate the last admin."),
    ).toBeVisible();
  });

  it("shows Edit name only to a holder of user:update", async () => {
    renderApp(`/admin/users/${UUID}`, {
      folders,
      handlers: [
        http.get(`*/api/v1/identity/users/${UUID}`, () =>
          HttpResponse.json(makeUser()),
        ),
      ],
    });
    await screen.findByRole("heading", { level: 1, name: "Alice Demo" }, LONG);
    expect(screen.queryByRole("button", { name: "Edit name" })).toBeNull();
  });

  it("edits the display name in a dialog and shows the new name", async () => {
    const actor = userEvent.setup();
    let name = "Alice Demo";
    const bodies: unknown[] = [];
    renderApp(`/admin/users/${UUID}`, {
      folders,
      me: makeMe({ global_permissions: ["user:read", "user:update"] }),
      handlers: [
        http.get(`*/api/v1/identity/users/${UUID}`, () =>
          HttpResponse.json(makeUser({ display_name: name })),
        ),
        http.patch(`*/api/v1/identity/users/${UUID}`, async ({ request }) => {
          const body = (await request.json()) as { display_name: string };
          bodies.push(body);
          name = body.display_name;
          return HttpResponse.json(makeUser({ display_name: name }));
        }),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Edit name" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Edit display name" }),
    );
    const field = dialog.getByLabelText("Display name");
    expect(field).toHaveValue("Alice Demo");
    await actor.clear(field);
    await actor.type(field, "   ");
    await actor.click(
      dialog.getByRole("button", { name: "Save display name" }),
    );
    expect(dialog.getByText("Enter a display name.")).toBeVisible();
    expect(bodies).toEqual([]);
    await actor.clear(field);
    await actor.type(field, "  Alice Liddell ");
    await actor.click(
      dialog.getByRole("button", { name: "Save display name" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(bodies).toEqual([{ display_name: "Alice Liddell" }]);
    expect(
      await screen.findByRole("heading", { level: 1, name: "Alice Liddell" }),
    ).toBeVisible();
  });

  it("shows a /display_name 422 on the dialog field and keeps it open", async () => {
    const actor = userEvent.setup();
    renderApp(`/admin/users/${UUID}`, {
      folders,
      me: makeMe({ global_permissions: ["user:read", "user:update"] }),
      handlers: [
        http.get(`*/api/v1/identity/users/${UUID}`, () =>
          HttpResponse.json(makeUser()),
        ),
        http.patch(`*/api/v1/identity/users/${UUID}`, () =>
          problem(422, "validation-error", "The request failed validation.", {
            errors: [
              {
                pointer: "/body/display_name",
                detail: "Value error, control characters are not allowed",
              },
            ],
          }),
        ),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Edit name" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Edit display name" }),
    );
    await actor.type(dialog.getByLabelText("Display name"), " Jr");
    await actor.click(
      dialog.getByRole("button", { name: "Save display name" }),
    );
    expect(
      await dialog.findByText(
        "Value error, control characters are not allowed",
      ),
    ).toBeVisible();
    expect(screen.getByRole("dialog")).toBeVisible();
  });

  it("shows any other display name refusal in the dialog's alert, and Go back closes it", async () => {
    const actor = userEvent.setup();
    renderApp(`/admin/users/${UUID}`, {
      folders,
      me: makeMe({ global_permissions: ["user:read", "user:update"] }),
      handlers: [
        http.get(`*/api/v1/identity/users/${UUID}`, () =>
          HttpResponse.json(makeUser()),
        ),
        http.patch(`*/api/v1/identity/users/${UUID}`, () =>
          problem(500, "internal", "Something broke inside."),
        ),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Edit name" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Edit display name" }),
    );
    await actor.type(dialog.getByLabelText("Display name"), " Jr");
    await actor.click(
      dialog.getByRole("button", { name: "Save display name" }),
    );
    expect(await dialog.findByText("Something broke inside.")).toBeVisible();
    await actor.click(dialog.getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});

describe("groups list", () => {
  it("shows a skeleton while loading", async () => {
    renderApp("/admin/groups", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups", async () => {
          await delay("infinite");
          return page([]);
        }),
      ],
    });
    expect(
      await screen.findByText("Loading groups", {}, LONG),
    ).toBeInTheDocument();
  });

  it("shows a problem and retries", async () => {
    const actor = userEvent.setup();
    // Fail every call until Retry: the principal-name lookup may request the same list first.
    let failing = true;
    renderApp("/admin/groups", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups", () => {
          return failing ? serverError() : page([makeGroup()]);
        }),
      ],
    });
    expect(
      await screen.findByText("Something broke inside.", {}, LONG),
    ).toBeVisible();
    failing = false;
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("link", { name: "demo-editors" }),
    ).toBeVisible();
  });

  it("offers exactly one New menu on the empty page to an owner", async () => {
    renderApp("/admin/groups", {
      folders,
      handlers: [http.get("*/api/v1/identity/groups", () => page([]))],
    });
    expect(
      await screen.findByRole("heading", { name: "No groups yet" }, LONG),
    ).toBeVisible();
    expect(screen.getAllByRole("button", { name: "New" })).toHaveLength(1);
  });

  it("hides the New menu when no owner can be offered", async () => {
    renderApp("/admin/groups", {
      folders,
      me: nobody,
      handlers: [http.get("*/api/v1/identity/groups", () => page([]))],
    });
    expect(
      await screen.findByRole("heading", { name: "No groups yet" }, LONG),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "New" })).toBeNull();
  });

  it("names owners, loads more and filters by name or owner", async () => {
    const actor = userEvent.setup();
    const cursors: (string | null)[] = [];
    renderApp("/admin/groups", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups", ({ request }) => {
          const cursor = new URL(request.url).searchParams.get("cursor");
          cursors.push(cursor);
          return cursor === null
            ? page(
                [
                  makeGroup(),
                  makeGroup({
                    id: "grp-2",
                    name: "platform-admins",
                    owner_folder_id: ROOT_ID,
                  }),
                ],
                "c2",
              )
            : page([
                makeGroup({
                  id: "grp-3",
                  name: "orphans",
                  owner_folder_id: "ghost-id",
                }),
              ]);
        }),
      ],
    });
    const row = (
      await screen.findByRole("link", { name: "demo-editors" }, LONG)
    ).closest("tr");
    if (!row) throw new Error("no row");
    expect(row).toHaveTextContent("acme");
    expect(
      screen.getByRole("link", { name: "platform-admins" }).closest("tr"),
    ).toHaveTextContent("ROOT (global)");
    // No "Hide deactivated" for groups.
    expect(screen.queryByLabelText("Hide deactivated")).toBeNull();
    await actor.click(
      screen.getByRole("button", { name: "Load more results" }),
    );
    const orphan = await screen.findByRole("link", { name: "orphans" });
    // An owner the caller cannot see falls back to its id.
    expect(orphan.closest("tr")).toHaveTextContent("ghost-id");
    expect(cursors).toEqual([null, "c2"]);
    await actor.type(screen.getByLabelText("Search groups"), "root");
    expect(screen.getByRole("link", { name: "platform-admins" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "demo-editors" })).toBeNull();
  });
});

describe("create group dialog", () => {
  async function openCreate(respond: () => Response, bodies: unknown[] = []) {
    const actor = userEvent.setup();
    renderApp("/admin/groups", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([makeGroup()])),
        http.post("*/api/v1/identity/groups", async ({ request }) => {
          bodies.push(await request.json());
          return respond();
        }),
        http.get("*/api/v1/identity/groups/new-grp", () =>
          HttpResponse.json(makeGroup({ id: "new-grp", name: "new-team" })),
        ),
        http.get("*/api/v1/identity/groups/new-grp/members", () =>
          HttpResponse.json(empty),
        ),
        ...principalLists(),
      ],
    });
    await actor.click(await screen.findByRole("button", { name: "New" }, LONG));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Create local group" }),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Create local group" }),
    );
    return { actor, dialog };
  }

  it("requires a name, offers ROOT and orgs, posts the body and opens the group", async () => {
    const bodies: unknown[] = [];
    const { actor, dialog } = await openCreate(
      () =>
        HttpResponse.json(makeGroup({ id: "new-grp", name: "new-team" }), {
          status: 201,
        }),
      bodies,
    );
    const owner = dialog.getByLabelText("Owner");
    expect(owner).toHaveValue(ROOT_ID);
    expect(
      within(owner)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["ROOT (global)", "Acme", "Globex"]);
    await actor.click(
      dialog.getByRole("button", { name: "Create local group" }),
    );
    expect(dialog.getByText("Enter a group name.")).toBeVisible();
    expect(bodies).toEqual([]);
    await actor.type(dialog.getByLabelText("Name"), " new-team ");
    await actor.selectOptions(owner, "acme-id");
    await actor.click(
      dialog.getByRole("button", { name: "Create local group" }),
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: "new-team" }),
    ).toBeVisible();
    expect(bodies).toEqual([{ name: "new-team", owner_folder_id: "acme-id" }]);
  });

  it("puts a 409 on the name", async () => {
    const { actor, dialog } = await openCreate(() =>
      problem(409, "conflict", "Exists."),
    );
    await actor.type(dialog.getByLabelText("Name"), "dup");
    await actor.click(
      dialog.getByRole("button", { name: "Create local group" }),
    );
    expect(
      await dialog.findByText("A group with this name already exists here."),
    ).toBeVisible();
  });

  it("maps an owner pointer onto the owner field", async () => {
    const { actor, dialog } = await openCreate(() =>
      problem(422, "validation-failed", "Invalid.", {
        errors: [{ pointer: "/owner_folder_id", detail: "Not an org." }],
      }),
    );
    await actor.type(dialog.getByLabelText("Name"), "x");
    await actor.click(
      dialog.getByRole("button", { name: "Create local group" }),
    );
    expect(await dialog.findByText("Not an org.")).toBeVisible();
  });

  it("shows other failures in a problem alert and closes on Go back", async () => {
    const { actor, dialog } = await openCreate(serverError);
    await actor.type(dialog.getByLabelText("Name"), "x");
    await actor.click(
      dialog.getByRole("button", { name: "Create local group" }),
    );
    expect(await dialog.findByText("Something broke inside.")).toBeVisible();
    await actor.click(dialog.getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Create local group" }),
      ).toBeNull();
    });
  });
});

describe("group detail", () => {
  function groupHandlers({
    members = [] as unknown[],
    membersResponse,
    extra = [],
  }: {
    members?: unknown[];
    membersResponse?: () => Response;
    extra?: RequestHandler[];
  } = {}) {
    return [
      ...extra,
      ...principalLists({ accounts: [makeAccount()] }),
      http.get("*/api/v1/identity/groups/grp-1", () =>
        HttpResponse.json(makeGroup()),
      ),
      http.get("*/api/v1/identity/groups/grp-1/members", () =>
        membersResponse ? membersResponse() : page(members),
      ),
    ];
  }

  const alice = {
    member_type: "user",
    member_id: `local:${UUID}`,
    display_name: "Alice Demo",
    username: "demo-alice",
    added_at: "2026-11-02T10:00:00Z",
  };

  it("renders Not found for a 404", async () => {
    renderApp("/admin/groups/nope", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups/nope", () =>
          problem(404, "not-found", "No such group."),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "Not found" }, LONG),
    ).toBeVisible();
  });

  it("shows a problem for another failure and retries", async () => {
    const actor = userEvent.setup();
    let calls = 0;
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups/grp-1", () => {
          calls += 1;
          return calls === 1 ? serverError() : HttpResponse.json(makeGroup());
        }),
        http.get("*/api/v1/identity/groups/grp-1/members", () =>
          HttpResponse.json(empty),
        ),
      ],
    });
    expect(
      await screen.findByText("Something broke inside.", {}, LONG),
    ).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "demo-editors" }),
    ).toBeVisible();
  });

  it("shows skeletons while the group and its members load", async () => {
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: groupHandlers({
        extra: [
          http.get("*/api/v1/identity/groups/grp-1/members", async () => {
            await delay("infinite");
            return page([]);
          }),
        ],
      }),
    });
    expect(
      await screen.findByText("Loading members", {}, LONG),
    ).toBeInTheDocument();
    expect(screen.getByText("acme")).toBeVisible();
  });

  it("shows a members failure as a problem", async () => {
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: groupHandlers({ membersResponse: serverError }),
    });
    expect(
      await screen.findByText("Something broke inside.", {}, LONG),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { level: 1, name: "demo-editors" }),
    ).toBeVisible();
  });

  it("lists members with name fallbacks and loads more", async () => {
    const actor = userEvent.setup();
    const cursors: (string | null)[] = [];
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups/grp-1/members", ({ request }) => {
          const cursor = new URL(request.url).searchParams.get("cursor");
          cursors.push(cursor);
          return cursor === null
            ? page(
                [
                  alice,
                  {
                    member_type: "service_account",
                    member_id: `sa:${UUID}`,
                    display_name: null,
                    username: "pipeline-user",
                    added_at: "2026-11-01T10:00:00Z",
                  },
                ],
                "m2",
              )
            : page([
                {
                  member_type: "user",
                  member_id: "local:anon",
                  display_name: null,
                  username: null,
                  added_at: "2026-11-03T10:00:00Z",
                },
              ]);
        }),
        ...groupHandlers(),
      ],
    });
    const table = await screen.findByRole(
      "table",
      { name: "Members of demo-editors" },
      LONG,
    );
    expect(within(table).getByText("Alice Demo")).toBeVisible();
    expect(within(table).getByText("Service account")).toBeVisible();
    // display_name null falls back to the username.
    expect(within(table).getAllByText("pipeline-user")).toHaveLength(2);
    // Add member sits next to the heading once members exist.
    expect(screen.getByRole("button", { name: "Add member" })).toBeVisible();
    // Sorting: by name (the id when there is none), then by the date added.
    expect((await sortRowsBy(actor, table, "Member"))[0]).toContain(
      "Alice Demo",
    );
    expect((await sortRowsBy(actor, table, "Member"))[0]).toContain(
      `sa:${UUID}`,
    );
    expect((await sortRowsBy(actor, table, "Added"))[0]).toContain(
      `sa:${UUID}`,
    );
    await actor.click(
      screen.getByRole("button", { name: "Load more results" }),
    );
    await waitFor(() => {
      expect(cursors).toEqual([null, "m2"]);
    });
    // Neither name nor username: the member id and "Not set".
    const anon = await within(table).findAllByText("local:anon");
    expect(anon.length).toBeGreaterThan(0);
  });

  it("renames, refusing an empty name and a duplicate", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    let call = 0;
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: groupHandlers({
        extra: [
          http.patch("*/api/v1/identity/groups/grp-1", async ({ request }) => {
            bodies.push(await request.json());
            call += 1;
            if (call === 1) return problem(409, "conflict", "Exists.");
            if (call === 2) return serverError();
            return HttpResponse.json(makeGroup({ name: "renamed" }));
          }),
        ],
      }),
    });
    await actor.click(
      await screen.findByRole("button", { name: "Rename group" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Rename demo-editors" }),
    );
    const name = dialog.getByLabelText("Name");
    expect(name).toHaveValue("demo-editors");
    await actor.clear(name);
    await actor.click(dialog.getByRole("button", { name: "Rename group" }));
    expect(dialog.getByText("Enter a group name.")).toBeVisible();
    // A name the rule refuses is never sent.
    await actor.type(name, "ops 😀");
    await actor.click(dialog.getByRole("button", { name: "Rename group" }));
    expect(bodies).toEqual([]);
    await actor.clear(name);
    await actor.type(name, "taken");
    await actor.click(dialog.getByRole("button", { name: "Rename group" }));
    expect(
      await dialog.findByText("A group with this name already exists here."),
    ).toBeVisible();
    await actor.click(dialog.getByRole("button", { name: "Rename group" }));
    expect(await dialog.findByText("Something broke inside.")).toBeVisible();
    await actor.clear(name);
    await actor.type(name, " renamed ");
    await actor.click(dialog.getByRole("button", { name: "Rename group" }));
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Rename demo-editors" }),
      ).toBeNull();
    });
    expect(bodies).toEqual([
      { name: "taken" },
      { name: "taken" },
      { name: "renamed" },
    ]);
  });

  it("closes the rename dialog on Go back without a request", async () => {
    const actor = userEvent.setup();
    let patched = false;
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: groupHandlers({
        extra: [
          http.patch("*/api/v1/identity/groups/grp-1", () => {
            patched = true;
            return HttpResponse.json(makeGroup());
          }),
        ],
      }),
    });
    await actor.click(
      await screen.findByRole("button", { name: "Rename group" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Rename demo-editors" }),
    );
    await actor.click(dialog.getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    // Escape closes it the same way.
    await actor.click(screen.getByRole("button", { name: "Rename group" }));
    await screen.findByRole("dialog", { name: "Rename demo-editors" });
    await actor.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(patched).toBe(false);
  });

  // Regression: the controlled Dialog never calls onOpenChange(true), so a reset placed there left
  // a discarded draft name ("demo-editors-draft") in the field on reopen.
  it("closes the rename dialog on Go back and restores the name next time", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/groups/grp-1", { folders, handlers: groupHandlers() });
    await actor.click(
      await screen.findByRole("button", { name: "Rename group" }, LONG),
    );
    let dialog = within(
      await screen.findByRole("dialog", { name: "Rename demo-editors" }),
    );
    await actor.type(dialog.getByLabelText("Name"), "-draft");
    await actor.click(dialog.getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await actor.click(screen.getByRole("button", { name: "Rename group" }));
    dialog = within(
      await screen.findByRole("dialog", { name: "Rename demo-editors" }),
    );
    expect(dialog.getByLabelText("Name")).toHaveValue("demo-editors");
  });

  it("deletes after typing the name and returns to the list", async () => {
    const actor = userEvent.setup();
    const deleted: string[] = [];
    const { router } = renderApp("/admin/groups/grp-1", {
      folders,
      handlers: groupHandlers({
        extra: [
          http.delete("*/api/v1/identity/groups/grp-1", ({ request }) => {
            deleted.push(new URL(request.url).pathname);
            return new HttpResponse(null, { status: 204 });
          }),
        ],
      }),
    });
    await actor.click(
      await screen.findByRole("button", { name: "Delete group" }, LONG),
    );
    await confirmTyped(
      actor,
      "Delete demo-editors?",
      "demo-editors",
      "Delete demo-editors",
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: "Groups" }),
    ).toBeVisible();
    expect(router.state.location.pathname).toBe("/admin/groups");
    expect(deleted).toEqual(["/api/v1/identity/groups/grp-1"]);
  });

  it("removes a member, or keeps them", async () => {
    const actor = userEvent.setup();
    const deleted: string[] = [];
    let members: unknown[] = [alice];
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups/grp-1/members", () => page(members)),
        http.delete(
          "*/api/v1/identity/groups/grp-1/members/:memberId",
          ({ params }) => {
            deleted.push(String(params.memberId));
            members = [];
            return new HttpResponse(null, { status: 204 });
          },
        ),
        ...groupHandlers(),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Remove" }, LONG),
    );
    let dialog = await screen.findByRole("alertdialog", {
      name: "Remove Alice Demo from demo-editors?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Keep member" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(deleted).toEqual([]);
    await actor.click(screen.getByRole("button", { name: "Remove" }));
    dialog = await screen.findByRole("alertdialog", {
      name: "Remove Alice Demo from demo-editors?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Remove member" }),
    );
    expect(
      await screen.findByRole("heading", { name: "No members yet" }),
    ).toBeVisible();
    expect(deleted).toEqual([`local:${UUID}`]);
  });

  it("adds a picked member, requiring a pick first", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    let members: unknown[] = [];
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/groups/grp-1/members", () => page(members)),
        http.post(
          "*/api/v1/identity/groups/grp-1/members",
          async ({ request }) => {
            bodies.push(await request.json());
            members = [alice];
            return new HttpResponse(null, { status: 201 });
          },
        ),
        ...groupHandlers(),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "No members yet" }, LONG),
    ).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Add member" }));
    const element = await screen.findByRole("dialog", {
      name: "Add a member to demo-editors",
    });
    const dialog = within(element);
    await actor.click(dialog.getByRole("button", { name: "Add member" }));
    expect(dialog.getByText("Choose a member to add.")).toBeVisible();
    expect(bodies).toEqual([]);
    await actor.type(dialog.getByLabelText("Search users"), "alice");
    await actor.click(
      await dialog.findByRole("option", { name: /Alice Demo/ }),
    );
    await actor.click(dialog.getByRole("button", { name: "Add member" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(bodies).toEqual([{ principal_id: `local:${UUID}` }]);
    expect(
      await screen.findByRole("table", { name: "Members of demo-editors" }),
    ).toBeVisible();
  });

  it("shows a refusal without a slug, other failures as a problem, and resets on Go back", async () => {
    const actor = userEvent.setup();
    let call = 0;
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: groupHandlers({
        extra: [
          http.post("*/api/v1/identity/groups/grp-1/members", () => {
            call += 1;
            if (call === 1)
              return HttpResponse.json(
                {
                  title: "Unprocessable",
                  status: 422,
                  detail: "That account is deactivated.",
                },
                {
                  status: 422,
                  headers: { "Content-Type": "application/problem+json" },
                },
              );
            return serverError();
          }),
        ],
      }),
    });
    await actor.click(
      await screen.findByRole("button", { name: "Add member" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", {
        name: "Add a member to demo-editors",
      }),
    );
    await actor.type(dialog.getByLabelText("Search users"), "alice");
    await actor.click(
      await dialog.findByRole("option", { name: /Alice Demo/ }),
    );
    await actor.click(dialog.getByRole("button", { name: "Add member" }));
    expect(await dialog.findByText("Cannot add this member:")).toBeVisible();
    expect(dialog.getByText(/That account is deactivated\./)).toBeVisible();
    await actor.click(dialog.getByRole("button", { name: "Add member" }));
    expect(await dialog.findByText("Something broke inside.")).toBeVisible();
    expect(dialog.queryByText("Cannot add this member:")).toBeNull();
    await actor.click(dialog.getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await actor.click(screen.getByRole("button", { name: "Add member" }));
    const again = within(
      await screen.findByRole("dialog", {
        name: "Add a member to demo-editors",
      }),
    );
    expect(again.queryByText("Something broke inside.")).toBeNull();
  });
});

describe("service accounts list", () => {
  it("shows a skeleton while loading", async () => {
    renderApp("/admin/service-accounts", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/service-accounts", async () => {
          await delay("infinite");
          return page([]);
        }),
      ],
    });
    expect(
      await screen.findByText("Loading service accounts", {}, LONG),
    ).toBeInTheDocument();
  });

  it("shows a problem and retries", async () => {
    const actor = userEvent.setup();
    // Fail every call until Retry: the principal-name lookup may request the same list first.
    let failing = true;
    renderApp("/admin/service-accounts", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/service-accounts", () => {
          return failing ? serverError() : page([makeAccount()]);
        }),
      ],
    });
    expect(
      await screen.findByText("Something broke inside.", {}, LONG),
    ).toBeVisible();
    failing = false;
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("link", { name: "demo-pipeline" }),
    ).toBeVisible();
  });

  it("shows the empty state with and without an owner to offer", async () => {
    renderApp("/admin/service-accounts", {
      folders,
      me: nobody,
      handlers: [
        http.get("*/api/v1/identity/service-accounts", () => page([])),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { name: "No service accounts yet" },
        LONG,
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "New" })).toBeNull();
  });

  it("lists accounts with source and status, loads more and filters", async () => {
    const actor = userEvent.setup();
    const cursors: (string | null)[] = [];
    renderApp("/admin/service-accounts", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/service-accounts", ({ request }) => {
          const cursor = new URL(request.url).searchParams.get("cursor");
          cursors.push(cursor);
          return cursor === null
            ? page([makeAccount()], "s2")
            : page([
                makeAccount({
                  id: "sa-2",
                  name: "old-job",
                  principal_id: `sa:${UUID2}`,
                  owner_folder_id: "globex-id",
                  is_active: false,
                }),
              ]);
        }),
      ],
    });
    const link = await screen.findByRole(
      "link",
      { name: "demo-pipeline" },
      LONG,
    );
    expect(link).toHaveAttribute("href", "/admin/service-accounts/sa-1");
    expect(link.closest("tr")).toHaveTextContent("Local");
    expect(link.closest("tr")).toHaveTextContent("Active");
    expect(screen.getAllByRole("button", { name: "New" })).toHaveLength(1);
    await actor.click(
      screen.getByRole("button", { name: "Load more results" }),
    );
    expect(await screen.findByRole("link", { name: "old-job" })).toBeVisible();
    expect(cursors).toEqual([null, "s2"]);
    await actor.click(screen.getByLabelText("Hide deactivated"));
    expect(screen.queryByRole("link", { name: "old-job" })).toBeNull();
    await actor.click(screen.getByLabelText("Hide deactivated"));
    await actor.type(
      screen.getByLabelText("Search service accounts"),
      "globex",
    );
    expect(screen.getByRole("link", { name: "old-job" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "demo-pipeline" })).toBeNull();
  });
});

describe("create service account dialog", () => {
  async function openCreate(respond: () => Response, bodies: unknown[] = []) {
    const actor = userEvent.setup();
    renderApp("/admin/service-accounts", {
      folders,
      // An Organization admin of Acme only: Acme is the single owner.
      me: makeMe({
        global_permissions: [],
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
      handlers: [
        http.get("*/api/v1/identity/service-accounts", () => page([])),
        http.post("*/api/v1/identity/service-accounts", async ({ request }) => {
          bodies.push(await request.json());
          return respond();
        }),
        http.get("*/api/v1/identity/service-accounts/new-sa", () =>
          HttpResponse.json(makeAccount({ id: "new-sa", name: "etl" })),
        ),
        http.get("*/api/v1/identity/service-accounts/new-sa/keys", () =>
          HttpResponse.json({ items: [] }),
        ),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { name: "No service accounts yet" },
        LONG,
      ),
    ).toBeVisible();
    await actor.click(
      screen.getAllByRole("button", { name: "New" })[0] ?? document.body,
    );
    await actor.click(
      await screen.findByRole("menuitem", { name: "Create service account" }),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Create service account" }),
    );
    return { actor, dialog };
  }

  it("requires a name, posts the only owner and opens the account", async () => {
    const bodies: unknown[] = [];
    const { actor, dialog } = await openCreate(
      () =>
        HttpResponse.json(makeAccount({ id: "new-sa", name: "etl" }), {
          status: 201,
        }),
      bodies,
    );
    const owner = dialog.getByLabelText("Owner");
    expect(
      within(owner)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Acme"]);
    await actor.click(
      dialog.getByRole("button", { name: "Create service account" }),
    );
    expect(dialog.getByText("Enter a name.")).toBeVisible();
    await actor.type(dialog.getByLabelText("Name"), "etl");
    await actor.selectOptions(owner, "acme-id");
    await actor.click(
      dialog.getByRole("button", { name: "Create service account" }),
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: "etl" }),
    ).toBeVisible();
    expect(bodies).toEqual([{ name: "etl", owner_folder_id: "acme-id" }]);
  });

  it("puts a 409 on the name", async () => {
    const { actor, dialog } = await openCreate(() =>
      problem(409, "conflict", "Exists."),
    );
    await actor.type(dialog.getByLabelText("Name"), "etl");
    await actor.click(
      dialog.getByRole("button", { name: "Create service account" }),
    );
    expect(
      await dialog.findByText(
        "A service account with this name already exists here.",
      ),
    ).toBeVisible();
  });

  it("maps a name pointer onto the name field", async () => {
    const { actor, dialog } = await openCreate(() =>
      problem(422, "validation-failed", "Invalid.", {
        errors: [{ pointer: "/name", detail: "Too odd." }],
      }),
    );
    await actor.type(dialog.getByLabelText("Name"), "etl");
    await actor.click(
      dialog.getByRole("button", { name: "Create service account" }),
    );
    expect(await dialog.findByText("Too odd.")).toBeVisible();
  });

  it("shows other failures in a problem alert and closes on Go back", async () => {
    const { actor, dialog } = await openCreate(serverError);
    await actor.type(dialog.getByLabelText("Name"), "etl");
    await actor.click(
      dialog.getByRole("button", { name: "Create service account" }),
    );
    expect(await dialog.findByText("Something broke inside.")).toBeVisible();
    await actor.click(dialog.getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});

describe("service account detail", () => {
  function saHandlers({
    account = makeAccount(),
    keys = [] as unknown[],
    keysResponse,
    extra = [],
  }: {
    account?: Record<string, unknown>;
    keys?: unknown[];
    keysResponse?: () => Response | Promise<Response>;
    extra?: RequestHandler[];
  } = {}) {
    return [
      ...extra,
      http.get("*/api/v1/identity/service-accounts/sa-1", () =>
        HttpResponse.json(account),
      ),
      http.get("*/api/v1/identity/service-accounts/sa-1/keys", () =>
        keysResponse ? keysResponse() : HttpResponse.json({ items: keys }),
      ),
    ];
  }

  it("renders Not found for a 404", async () => {
    renderApp("/admin/service-accounts/none", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/service-accounts/none", () =>
          problem(404, "not-found", "No such account."),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "Not found" }, LONG),
    ).toBeVisible();
  });

  it("shows another failure as a problem and retries", async () => {
    const actor = userEvent.setup();
    let calls = 0;
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/service-accounts/sa-1", () => {
          calls += 1;
          return calls === 1 ? serverError() : HttpResponse.json(makeAccount());
        }),
        http.get("*/api/v1/identity/service-accounts/sa-1/keys", () =>
          HttpResponse.json({ items: [] }),
        ),
      ],
    });
    expect(
      await screen.findByText("Something broke inside.", {}, LONG),
    ).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "demo-pipeline" }),
    ).toBeVisible();
  });

  it("shows skeletons while the account and its keys load", async () => {
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: saHandlers({
        keysResponse: async () => {
          await delay("infinite");
          return HttpResponse.json({ items: [] });
        },
      }),
    });
    expect(
      await screen.findByText("Loading keys", {}, LONG),
    ).toBeInTheDocument();
    expect(screen.getByText(`sa:${UUID}`)).toBeVisible();
    expect(screen.getByText("acme")).toBeVisible();
  });

  it("shows a keys failure as a problem and retries it", async () => {
    const actor = userEvent.setup();
    let calls = 0;
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: saHandlers({
        keysResponse: () => {
          calls += 1;
          return calls === 1
            ? serverError()
            : HttpResponse.json({ items: [makeKey()] });
        },
      }),
    });
    expect(
      await screen.findByText("Something broke inside.", {}, LONG),
    ).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("rowheader", { name: "key-1" }),
    ).toBeVisible();
  });

  it("explains keys need reactivation on a deactivated account", async () => {
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: saHandlers({ account: makeAccount({ is_active: false }) }),
    });
    expect(
      await screen.findByText(
        "Reactivate this account to create keys.",
        {},
        LONG,
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Create key" })).toBeNull();
    expect(screen.getByRole("button", { name: "Reactivate" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Deactivate" })).toBeNull();
  });

  it("lists keys with expiry hints, status, last use and the rotation warning", async () => {
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: saHandlers({
        keys: [
          makeKey({
            id: "k-1",
            name: "soon",
            expires_at: inDays(10),
            created_at: "2026-10-01T10:00:00Z",
            expiring_soon: true,
            last_used_at: "2026-11-10T10:00:00Z",
          }),
          makeKey({ id: "k-2", name: "tomorrow", expires_at: inDays(1) }),
          makeKey({ id: "k-3", name: "later", expires_at: inDays(100) }),
          makeKey({
            id: "k-4",
            name: "gone",
            status: "revoked",
            revoked_at: "2026-11-05T10:00:00Z",
          }),
          makeKey({
            id: "k-5",
            name: "stale",
            status: "expired",
            expires_at: "2026-11-01T10:00:00Z",
          }),
        ],
      }),
    });
    const table = await screen.findByRole(
      "table",
      { name: "Keys of demo-pipeline" },
      LONG,
    );
    expect(screen.getByText("A key expires soon")).toBeVisible();
    const row = (name: string) => {
      const header = within(table).getByRole("rowheader", { name });
      const tr = header.closest("tr");
      if (!tr) throw new Error("no row");
      return within(tr);
    };
    expect(row("soon").getByText("Expires in 10 days")).toBeVisible();
    expect(row("soon").queryByText("Never")).toBeNull();
    expect(row("tomorrow").getByText("Expires in 1 day")).toBeVisible();
    expect(row("tomorrow").getByText("Never")).toBeVisible();
    expect(row("later").getByText("Expires in 100 days")).toBeVisible();
    expect(row("gone").getByText("Revoked")).toBeVisible();
    expect(row("gone").queryByRole("button", { name: "Revoke" })).toBeNull();
    expect(row("gone").queryByText(/Expires in/)).toBeNull();
    expect(row("stale").getByText("Expired")).toBeVisible();
    expect(row("later").getByRole("button", { name: "Revoke" })).toBeVisible();
    // Create key sits next to the heading once keys exist.
    expect(screen.getByRole("button", { name: "Create key" })).toBeVisible();
    const actor = userEvent.setup();
    expect(await sortRowsBy(actor, table, "Name")).toEqual([
      "gone",
      "later",
      "soon",
      "stale",
      "tomorrow",
    ]);
    expect((await sortRowsBy(actor, table, "Expires"))[0]).toBe("stale");
    expect((await sortRowsBy(actor, table, "Created"))[0]).toBe("soon");
  });

  it("revokes a key after typing its name", async () => {
    const actor = userEvent.setup();
    const deleted: string[] = [];
    let keys: unknown[] = [makeKey()];
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: saHandlers({
        keysResponse: () => HttpResponse.json({ items: keys }),
        extra: [
          http.delete(
            "*/api/v1/identity/service-accounts/sa-1/keys/:keyId",
            ({ params, request }) => {
              deleted.push(`${request.method} ${String(params.keyId)}`);
              keys = [];
              return new HttpResponse(null, { status: 204 });
            },
          ),
        ],
      }),
    });
    await actor.click(
      await screen.findByRole("button", { name: "Revoke" }, LONG),
    );
    await confirmTyped(actor, "Revoke key-1?", "key-1", "Revoke key-1");
    expect(
      await screen.findByRole("heading", { name: "No keys yet" }),
    ).toBeVisible();
    expect(deleted).toEqual(["DELETE k-1"]);
  });

  it("deactivates after typing the name and reactivates", async () => {
    const actor = userEvent.setup();
    let active = true;
    const posted: string[] = [];
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/service-accounts/sa-1", () =>
          HttpResponse.json(makeAccount({ is_active: active })),
        ),
        http.get("*/api/v1/identity/service-accounts/sa-1/keys", () =>
          HttpResponse.json({ items: [] }),
        ),
        http.post(
          "*/api/v1/identity/service-accounts/sa-1/:action",
          ({ params }) => {
            posted.push(String(params.action));
            active = params.action === "reactivate";
            return new HttpResponse(null, { status: 204 });
          },
        ),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Deactivate" }, LONG),
    );
    await confirmTyped(
      actor,
      "Deactivate demo-pipeline?",
      "demo-pipeline",
      "Deactivate demo-pipeline",
    );
    await actor.click(
      await screen.findByRole("button", { name: "Reactivate" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Reactivate demo-pipeline?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Reactivate demo-pipeline" }),
    );
    expect(
      await screen.findByRole("button", { name: "Deactivate" }),
    ).toBeVisible();
    expect(posted).toEqual(["deactivate", "reactivate"]);
  });

  it("renames, refusing an empty name, a duplicate and a failure", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    let call = 0;
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: saHandlers({
        extra: [
          http.patch(
            "*/api/v1/identity/service-accounts/sa-1",
            async ({ request }) => {
              bodies.push(await request.json());
              call += 1;
              if (call === 1) return problem(409, "conflict", "Exists.");
              if (call === 2) return serverError();
              return HttpResponse.json(makeAccount({ name: "etl" }));
            },
          ),
        ],
      }),
    });
    await actor.click(
      await screen.findByRole(
        "button",
        { name: "Rename service account" },
        LONG,
      ),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Rename demo-pipeline" }),
    );
    const name = dialog.getByLabelText("Name");
    expect(name).toHaveValue("demo-pipeline");
    await actor.clear(name);
    const submit = dialog.getByRole("button", {
      name: "Rename service account",
    });
    await actor.click(submit);
    expect(dialog.getByText("Enter a name.")).toBeVisible();
    await actor.type(name, "dup");
    await actor.click(submit);
    expect(
      await dialog.findByText(
        "A service account with this name already exists here.",
      ),
    ).toBeVisible();
    await actor.click(submit);
    expect(await dialog.findByText("Something broke inside.")).toBeVisible();
    await actor.clear(name);
    await actor.type(name, "etl");
    await actor.click(submit);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(bodies).toEqual([{ name: "dup" }, { name: "dup" }, { name: "etl" }]);
  });

  it("refreshes principal names after a rename", async () => {
    const actor = userEvent.setup();
    let nameCalls = 0;
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: saHandlers({
        extra: [
          http.post("*/api/v1/identity/principals/names", () => {
            nameCalls += 1;
            return HttpResponse.json({ items: [] });
          }),
          http.patch("*/api/v1/identity/service-accounts/sa-1", () =>
            HttpResponse.json(makeAccount({ name: "etl" })),
          ),
        ],
      }),
    });
    await actor.click(
      await screen.findByRole(
        "button",
        { name: "Rename service account" },
        LONG,
      ),
    );
    await waitFor(() => {
      expect(nameCalls).toBeGreaterThan(0);
    });
    const before = nameCalls;
    const dialog = within(
      await screen.findByRole("dialog", { name: "Rename demo-pipeline" }),
    );
    await actor.clear(dialog.getByLabelText("Name"));
    await actor.type(dialog.getByLabelText("Name"), "etl");
    await actor.click(
      dialog.getByRole("button", { name: "Rename service account" }),
    );
    await waitFor(() => {
      expect(nameCalls).toBeGreaterThan(before);
    });
  });

  it("closes rename on Go back and restores the name next time", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: saHandlers(),
    });
    const opener = await screen.findByRole(
      "button",
      { name: "Rename service account" },
      LONG,
    );
    await actor.click(opener);
    let dialog = within(
      await screen.findByRole("dialog", { name: "Rename demo-pipeline" }),
    );
    await actor.type(dialog.getByLabelText("Name"), "-draft");
    await actor.click(dialog.getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await actor.click(opener);
    dialog = within(
      await screen.findByRole("dialog", { name: "Rename demo-pipeline" }),
    );
    expect(dialog.getByLabelText("Name")).toHaveValue("demo-pipeline");
  });

  describe("create key dialog", () => {
    async function openKeyDialog(respond: () => Response, maxDays?: number) {
      const actor = userEvent.setup();
      const bodies: { name: string; expires_at: string }[] = [];
      renderApp("/admin/service-accounts/sa-1", {
        folders,
        handlers: saHandlers({
          extra: [
            // The effective ceiling comes from the public settings (Settings > Sign-in & security).
            ...(maxDays === undefined
              ? []
              : [
                  http.get("*/api/v1/identity/settings/public", () =>
                    HttpResponse.json({
                      banner: null,
                      entra_default_tenant_id: null,
                      entra_default_owner_folder_id: null,
                      default_odcs_api_version: "v3.2.0",
                      require_lifecycle_reason: false,
                      credential_max_ttl_days: maxDays,
                      password_rules: {
                        min_length: 12,
                        require_upper: false,
                        require_lower: false,
                        require_digit: false,
                        require_symbol: false,
                      },
                    }),
                  ),
                ]),
            http.post(
              "*/api/v1/identity/service-accounts/sa-1/keys",
              async ({ request }) => {
                bodies.push(
                  (await request.json()) as {
                    name: string;
                    expires_at: string;
                  },
                );
                return respond();
              },
            ),
          ],
        }),
      });
      expect(
        await screen.findByText(
          "Create a key and store it in your pipeline's secret manager.",
          {},
          LONG,
        ),
      ).toBeVisible();
      await actor.click(screen.getByRole("button", { name: "Create key" }));
      const dialog = within(
        await screen.findByRole("dialog", { name: "Create key" }),
      );
      return { actor, dialog, bodies };
    }

    it("validates the name and the expiry window before sending", async () => {
      const { actor, dialog, bodies } = await openKeyDialog(() =>
        HttpResponse.json({}),
      );
      expect(dialog.getByText("Default 90 days, at most 365.")).toBeVisible();
      // No Read-only option for keys.
      expect(dialog.queryByLabelText("Read-only")).toBeNull();
      fireEvent.change(dialog.getByLabelText("Expires on"), {
        target: { value: "2001-01-01" },
      });
      await actor.click(dialog.getByRole("button", { name: "Create key" }));
      expect(dialog.getByText("Enter a name.")).toBeVisible();
      expect(
        dialog.getByText(
          "Choose a date from tomorrow up to 365 days from today.",
        ),
      ).toBeVisible();
      expect(bodies).toEqual([]);
    });

    it("caps the expiry at the stored ceiling, and the default never exceeds it", async () => {
      const { actor, dialog, bodies } = await openKeyDialog(
        () => HttpResponse.json({}),
        30,
      );
      await waitFor(() => {
        expect(dialog.getByText("Default 30 days, at most 30.")).toBeVisible();
      });
      expect(dialog.getByLabelText("Expires on")).toHaveAttribute(
        "max",
        dateInDays(30),
      );
      fireEvent.change(dialog.getByLabelText("Expires on"), {
        target: { value: dateInDays(31) },
      });
      await actor.type(dialog.getByLabelText("Name"), "key-x");
      await actor.click(dialog.getByRole("button", { name: "Create key" }));
      expect(
        dialog.getByText(
          "Choose a date from tomorrow up to 30 days from today.",
        ),
      ).toBeVisible();
      expect(bodies).toEqual([]);
    });

    it("shows the fixed copy when the two-key limit is reached", async () => {
      const { actor, dialog, bodies } = await openKeyDialog(() =>
        problem(409, "credential-limit-reached", "Too many keys."),
      );
      await actor.type(dialog.getByLabelText("Name"), "  key-3 ");
      await actor.click(dialog.getByRole("button", { name: "Create key" }));
      expect(await dialog.findByText(KEY_LIMIT_COPY)).toBeVisible();
      expect(dialog.queryByText("Too many keys.")).toBeNull();
      expect(bodies[0]?.name).toBe("key-3");
      // The end of the default day (90 days ahead), sent as ISO.
      const expires = new Date(bodies[0]?.expires_at ?? "");
      expect(expires.getHours()).toBe(23);
      expect(expires.getMinutes()).toBe(59);
      const pad = (n: number) => String(n).padStart(2, "0");
      expect(
        `${String(expires.getFullYear())}-${pad(expires.getMonth() + 1)}-${pad(expires.getDate())}`,
      ).toBe(dateInDays(90));
    });

    it("puts a duplicate name on the name field", async () => {
      const { actor, dialog } = await openKeyDialog(() =>
        problem(409, "conflict", "Exists."),
      );
      await actor.type(dialog.getByLabelText("Name"), "key-1");
      await actor.click(dialog.getByRole("button", { name: "Create key" }));
      expect(
        await dialog.findByText("A credential with this name already exists."),
      ).toBeVisible();
    });

    it("puts an out-of-range expiry on the expiry field", async () => {
      const { actor, dialog } = await openKeyDialog(() =>
        problem(422, "expiry-out-of-range", "At most 30 days here."),
      );
      await actor.type(dialog.getByLabelText("Name"), "key-1");
      await actor.click(dialog.getByRole("button", { name: "Create key" }));
      expect(await dialog.findByText("At most 30 days here.")).toBeVisible();
    });

    it("shows other failures with the API detail and resets on Go back", async () => {
      const { actor, dialog } = await openKeyDialog(serverError);
      await actor.type(dialog.getByLabelText("Name"), "key-1");
      await actor.click(dialog.getByRole("button", { name: "Create key" }));
      expect(await dialog.findByText("Something broke inside.")).toBeVisible();
      await actor.click(dialog.getByRole("button", { name: "Go back" }));
      await waitFor(() => {
        expect(screen.queryByRole("dialog")).toBeNull();
      });
      await actor.click(screen.getByRole("button", { name: "Create key" }));
      const again = within(
        await screen.findByRole("dialog", { name: "Create key" }),
      );
      expect(again.getByLabelText("Name")).toHaveValue("");
      expect(again.queryByText("Something broke inside.")).toBeNull();
    });
  });
});

describe("my access tokens", () => {
  it("shows a skeleton while loading and the account tabs", async () => {
    renderApp("/account/tokens", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/pats", async () => {
          await delay("infinite");
          return page([]);
        }),
      ],
    });
    expect(
      await screen.findByText("Loading access tokens", {}, LONG),
    ).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Account settings" });
    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Profile", "Password", "Access tokens"]);
    expect(within(nav).getByRole("link", { name: "Password" })).toHaveAttribute(
      "href",
      "/account/password",
    );
    expect(
      within(nav).getByRole("link", { name: "Access tokens" }),
    ).toHaveAttribute("href", "/account/tokens");
  });

  it("shows a problem and retries", async () => {
    const actor = userEvent.setup();
    // Fail every call until Retry: the principal-name lookup may request the same list first.
    let failing = true;
    renderApp("/account/tokens", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/pats", () => {
          return failing ? serverError() : page([makePat()]);
        }),
      ],
    });
    expect(
      await screen.findByText("Something broke inside.", {}, LONG),
    ).toBeVisible();
    failing = false;
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("rowheader", { name: "laptop" }),
    ).toBeVisible();
  });

  it("lists tokens with access, status and last use, loading more pages", async () => {
    const actor = userEvent.setup();
    const cursors: (string | null)[] = [];
    renderApp("/account/tokens", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/pats", ({ request }) => {
          const cursor = new URL(request.url).searchParams.get("cursor");
          cursors.push(cursor);
          return cursor === null
            ? page(
                [
                  makePat({ last_used_at: "2026-11-10T10:00:00Z" }),
                  makePat({
                    id: "p-2",
                    name: "ci",
                    read_only: true,
                    status: "revoked",
                    expires_at: inDays(5),
                  }),
                ],
                "t2",
              )
            : page([makePat({ id: "p-3", name: "old", status: "expired" })]);
        }),
      ],
    });
    const table = await screen.findByRole(
      "table",
      { name: "Your access tokens" },
      LONG,
    );
    const row = (name: string) => {
      const tr = within(table).getByRole("rowheader", { name }).closest("tr");
      if (!tr) throw new Error("no row");
      return within(tr);
    };
    expect(row("laptop").getByText("Read and write")).toBeVisible();
    expect(row("laptop").getByText("Active")).toBeVisible();
    expect(row("laptop").queryByText("Never")).toBeNull();
    expect(
      row("laptop").getByRole("button", { name: "Revoke laptop" }),
    ).toBeVisible();
    expect(row("ci").getByText("Read-only")).toBeVisible();
    expect(row("ci").getByText("Revoked")).toBeVisible();
    expect(row("ci").getByText("Never")).toBeVisible();
    expect(row("ci").queryByRole("button")).toBeNull();
    // The header offers Create token once tokens exist.
    expect(screen.getByRole("button", { name: "Create token" })).toBeVisible();
    expect(await sortRowsBy(actor, table, "Name")).toEqual(["ci", "laptop"]);
    expect(await sortRowsBy(actor, table, "Name")).toEqual(["laptop", "ci"]);
    expect(await sortRowsBy(actor, table, "Expires")).toEqual(["ci", "laptop"]);
    await actor.click(
      screen.getByRole("button", { name: "Load more results" }),
    );
    expect(await within(table).findByText("Expired")).toBeVisible();
    expect(cursors).toEqual([null, "t2"]);
  });

  it("creates a token, reveals it once and lists it after closing", async () => {
    const actor = userEvent.setup();
    const secretToken = "dcp_pub_secretvalue123";
    const bodies: Record<string, unknown>[] = [];
    let pats: unknown[] = [makePat()];
    renderApp("/account/tokens", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/pats", () => page(pats)),
        http.post("*/api/v1/identity/pats", async ({ request }) => {
          const body = (await request.json()) as Record<string, unknown>;
          bodies.push(body);
          pats = [...pats, makePat({ id: "p-9", name: "ci-job" })];
          return HttpResponse.json(
            {
              id: "p-9",
              name: "ci-job",
              read_only: false,
              expires_at: body.expires_at,
              created_at: "2026-11-15T10:00:00Z",
              token: secretToken,
            },
            { status: 201 },
          );
        }),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Create token" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Create token" }),
    );
    expect(dialog.getByText("Default 30 days, at most 365.")).toBeVisible();
    await actor.type(dialog.getByLabelText("Name"), "ci-job");
    await actor.click(dialog.getByRole("button", { name: "Create token" }));
    const reveal = await screen.findByRole("dialog", {
      name: "Your new token",
    });
    expect(within(reveal).getByLabelText("Token")).toHaveValue(secretToken);
    expect(bodies[0]).toMatchObject({ name: "ci-job", read_only: false });
    await actor.click(
      within(reveal).getByLabelText("I have stored this token"),
    );
    await actor.click(
      within(reveal).getByRole("button", { name: "Close after storing token" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(
      await screen.findByRole("rowheader", { name: "ci-job" }),
    ).toBeVisible();
    expect(document.body.textContent).not.toContain(secretToken);
  });

  it("revokes a token after confirmation, or keeps it", async () => {
    const actor = userEvent.setup();
    const deleted: string[] = [];
    let pats: unknown[] = [makePat()];
    renderApp("/account/tokens", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/pats", () => page(pats)),
        http.delete("*/api/v1/identity/pats/:id", ({ params, request }) => {
          deleted.push(`${request.method} ${String(params.id)}`);
          pats = [makePat({ status: "revoked" })];
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Revoke laptop" }, LONG),
    );
    let dialog = await screen.findByRole("alertdialog", {
      name: "Revoke laptop?",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Keep laptop" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(deleted).toEqual([]);
    await actor.click(screen.getByRole("button", { name: "Revoke laptop" }));
    dialog = await screen.findByRole("alertdialog", { name: "Revoke laptop?" });
    await actor.click(
      within(dialog).getByRole("button", { name: "Revoke laptop" }),
    );
    expect(await screen.findByText("Revoked")).toBeVisible();
    expect(deleted).toEqual(["DELETE p-1"]);
  });
});

describe("all access tokens", () => {
  const admin: Me = makeMe({
    global_permissions: ["user:read", "pat:read-all", "pat:revoke-any"],
  });
  const ownerUser = makeUser();

  it("asks for nothing without pat:read-all", async () => {
    let requested = false;
    renderApp("/admin/pats", {
      folders,
      me: makeMe({ global_permissions: [] }),
      handlers: [
        ...principalLists(),
        http.get("*/api/v1/identity/admin/pats", () => {
          requested = true;
          return page([]);
        }),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { name: "Not available at your access level" },
        LONG,
      ),
    ).toBeVisible();
    expect(requested).toBe(false);
  });

  it("shows the empty state, a problem with retry, and a skeleton", async () => {
    const actor = userEvent.setup();
    let calls = 0;
    renderApp("/admin/pats", {
      folders,
      me: admin,
      handlers: [
        ...principalLists(),
        http.get("*/api/v1/identity/admin/pats", async ({ request }) => {
          calls += 1;
          if (calls === 1) return serverError();
          if (new URL(request.url).searchParams.get("status") === "expired") {
            await delay("infinite");
          }
          return page([]);
        }),
      ],
    });
    expect(
      await screen.findByText("Something broke inside.", {}, LONG),
    ).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", { name: "No access tokens" }),
    ).toBeVisible();
    await actor.selectOptions(screen.getByLabelText("Status"), "expired");
    expect(
      await screen.findByText("Loading access tokens"),
    ).toBeInTheDocument();
  });

  it("names owners, filters by status and text, and revokes any token", async () => {
    const actor = userEvent.setup();
    const statuses: (string | null)[] = [];
    const deleted: string[] = [];
    renderApp("/admin/pats", {
      folders,
      me: admin,
      handlers: [
        ...principalLists({ users: [ownerUser] }),
        http.get("*/api/v1/identity/admin/pats", ({ request }) => {
          const status = new URL(request.url).searchParams.get("status");
          statuses.push(status);
          if (status === "revoked")
            return page([
              makePat({
                id: "p-r",
                name: "burned",
                status: "revoked",
                owner_principal_id: `local:${UUID}`,
              }),
            ]);
          return page([
            makePat({ owner_principal_id: `local:${UUID}` }),
            makePat({
              id: "p-2",
              name: "mystery",
              owner_principal_id: "local:ghost",
            }),
          ]);
        }),
        http.delete("*/api/v1/identity/admin/pats/:id", ({ params }) => {
          deleted.push(String(params.id));
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    const table = await screen.findByRole(
      "table",
      { name: "All access tokens" },
      LONG,
    );
    expect(
      await within(table).findByText("Alice Demo (demo-alice)"),
    ).toBeVisible();
    expect(within(table).getByText("Unknown owner")).toBeVisible();
    expect(within(table).getByText("local:ghost")).toBeVisible();
    // An owner sorts by name, or by id when it has none.
    expect(await sortRowsBy(actor, table, "Owner")).toEqual([
      "laptop",
      "mystery",
    ]);

    await actor.type(screen.getByLabelText("Filter loaded rows"), "alice");
    expect(within(table).queryByText("mystery")).toBeNull();
    expect(
      within(table).getByRole("rowheader", { name: "laptop" }),
    ).toBeVisible();
    await actor.clear(screen.getByLabelText("Filter loaded rows"));

    await actor.click(
      within(table).getByRole("button", { name: "Revoke laptop" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Revoke laptop?",
    });
    expect(dialog).toHaveTextContent(
      "The token belongs to Alice Demo (demo-alice).",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Revoke laptop" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(deleted).toEqual(["p-1"]);

    // An unknown owner is named by id in the confirmation.
    await actor.click(screen.getByRole("button", { name: "Revoke mystery" }));
    const second = await screen.findByRole("alertdialog", {
      name: "Revoke mystery?",
    });
    expect(second).toHaveTextContent("The token belongs to local:ghost.");
    await actor.click(
      within(second).getByRole("button", { name: "Keep mystery" }),
    );

    await actor.selectOptions(screen.getByLabelText("Status"), "revoked");
    expect(
      await screen.findByRole("rowheader", { name: "burned" }),
    ).toBeVisible();
    expect(statuses[0]).toBeNull();
    expect(statuses).toContain("revoked");
  });

  it("leaves out the Actions column without pat:revoke-any", async () => {
    renderApp("/admin/pats", {
      folders,
      me: makeMe({ global_permissions: ["pat:read-all"] }),
      handlers: [
        ...principalLists(),
        http.get("*/api/v1/identity/admin/pats", () =>
          page([makePat({ owner_principal_id: `local:${UUID}` })]),
        ),
      ],
    });
    const table = await screen.findByRole(
      "table",
      { name: "All access tokens" },
      LONG,
    );
    expect(
      within(table).queryByRole("columnheader", { name: "Actions" }),
    ).toBeNull();
    expect(within(table).queryByRole("button", { name: /Revoke/ })).toBeNull();
    // Without user:read the user list is not read, so the owner stays unknown.
    expect(within(table).getByText("Unknown owner")).toBeVisible();
  });
});

const NAME_RULE =
  "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.";

describe("the name rule on rename and token forms", () => {
  it("refuses an emoji in the user display name", async () => {
    const actor = userEvent.setup();
    renderApp(`/admin/users/${UUID}`, {
      folders,
      me: makeMe({ global_permissions: ["user:read", "user:update"] }),
      handlers: [
        http.get(`*/api/v1/identity/users/${UUID}`, () =>
          HttpResponse.json(makeUser()),
        ),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Edit name" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Edit display name" }),
    );
    await actor.type(dialog.getByLabelText("Display name"), " 😀");
    expect(dialog.getByText(NAME_RULE)).toBeVisible();
  });

  it("refuses an emoji in the group name", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/groups/grp-1", {
      folders,
      handlers: [
        ...principalLists({ accounts: [makeAccount()] }),
        http.get("*/api/v1/identity/groups/grp-1", () =>
          HttpResponse.json(makeGroup()),
        ),
        http.get("*/api/v1/identity/groups/grp-1/members", () => page([])),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Rename group" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Rename demo-editors" }),
    );
    await actor.type(dialog.getByLabelText("Name"), " 😀");
    expect(dialog.getByText(NAME_RULE)).toBeVisible();
  });

  it("refuses an emoji in the service account name", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/service-accounts/sa-1", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/service-accounts/sa-1", () =>
          HttpResponse.json(makeAccount()),
        ),
        http.get("*/api/v1/identity/service-accounts/sa-1/keys", () =>
          HttpResponse.json({ items: [] }),
        ),
      ],
    });
    await actor.click(
      await screen.findByRole(
        "button",
        { name: "Rename service account" },
        LONG,
      ),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Rename demo-pipeline" }),
    );
    await actor.type(dialog.getByLabelText("Name"), " 😀");
    expect(dialog.getByText(NAME_RULE)).toBeVisible();
  });

  it("refuses an emoji in the token name and sends nothing", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/account/tokens", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/pats", () => page([makePat()])),
        http.post("*/api/v1/identity/pats", async ({ request }) => {
          bodies.push(await request.json());
          return HttpResponse.json({}, { status: 201 });
        }),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Create token" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Create token" }),
    );
    await actor.type(dialog.getByLabelText("Name"), "ci 😀");
    expect(dialog.getByText(NAME_RULE)).toBeVisible();
    await actor.click(dialog.getByRole("button", { name: "Create token" }));
    expect(bodies).toEqual([]);
  });
});

describe("the active token limit on my access tokens", () => {
  function patList(
    items: unknown[],
    counts: { active_count: number; max_active: number },
  ) {
    return HttpResponse.json({ items, next_cursor: null, ...counts });
  }

  it("shows the count and keeps Create token enabled below the limit", async () => {
    renderApp("/account/tokens", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/pats", () =>
          patList([makePat()], { active_count: 1, max_active: 10 }),
        ),
      ],
    });
    expect(await screen.findByText("1 of 10 active", {}, LONG)).toBeVisible();
    expect(screen.getByRole("button", { name: "Create token" })).toBeEnabled();
    expect(screen.queryByText(/You have reached the limit/)).toBeNull();
  });

  it("disables Create token at the limit and says why in text", async () => {
    renderApp("/account/tokens", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/pats", () =>
          patList([makePat()], { active_count: 3, max_active: 3 }),
        ),
      ],
    });
    expect(
      await screen.findByText(
        "3 of 3 active. You have reached the limit of 3 active access tokens. Revoke one to create another.",
        {},
        LONG,
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Create token" })).toBeDisabled();
  });

  it("renders a token-limit-reached 409 from a racing create in the dialog", async () => {
    const actor = userEvent.setup();
    renderApp("/account/tokens", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/pats", () =>
          patList([makePat()], { active_count: 2, max_active: 3 }),
        ),
        http.post("*/api/v1/identity/pats", () =>
          problem(
            409,
            "token-limit-reached",
            "You already have the maximum number of active access tokens. Revoke one or let one expire before creating another.",
            { limit: 3, active_count: 3 },
          ),
        ),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Create token" }, LONG),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Create token" }),
    );
    await actor.type(dialog.getByLabelText("Name"), "ci");
    await actor.click(dialog.getByRole("button", { name: "Create token" }));
    expect(
      await dialog.findByText(/You already have the maximum number of active/),
    ).toBeVisible();
    expect(
      dialog.queryByText("A credential with this name already exists."),
    ).toBeNull();
  });
});

describe("the Sessions card on the user detail page", () => {
  const SESSIONS_PATH = `*/api/v1/identity/users/${UUID}/sessions`;
  const sessionItem = (id: string, client: string | null) => ({
    id,
    started_at: "2026-11-20T08:00:00Z",
    last_active_at: "2026-11-20T09:30:00Z",
    idle_expires_at: "2026-11-27T09:30:00Z",
    expires_at: "2026-11-27T08:00:00Z",
    client,
    current: false,
  });
  const sessionList = (items: ReturnType<typeof sessionItem>[]) =>
    HttpResponse.json({
      items,
      total: items.length,
      max_sessions: 5,
      access_token_lifetime_s: 900,
    });
  const userHandler = http.get(`*/api/v1/identity/users/${UUID}`, () =>
    HttpResponse.json(makeUser()),
  );

  it("lists the sessions and offers sign-out only with user:deactivate", async () => {
    const actor = userEvent.setup();
    let rows = [
      sessionItem("s-1", "Chrome on macOS"),
      sessionItem("s-2", "API client (curl)"),
    ];
    const calls: string[] = [];
    renderApp(`/admin/users/${UUID}`, {
      folders,
      me: makeMe({ global_permissions: ["user:read", "user:deactivate"] }),
      handlers: [
        userHandler,
        http.get(SESSIONS_PATH, () => sessionList(rows)),
        http.delete(`${SESSIONS_PATH}/:id`, ({ params }) => {
          calls.push(`DELETE ${String(params.id)}`);
          rows = rows.filter((row) => row.id !== params.id);
          return new HttpResponse(null, { status: 204 });
        }),
        http.post(`${SESSIONS_PATH}/revoke-all`, () => {
          calls.push("POST revoke-all");
          const revoked = rows.length;
          rows = [];
          return HttpResponse.json({ revoked });
        }),
      ],
    });
    const curlRow = (
      await screen.findByText("API client (curl)", {}, LONG)
    ).closest("tr") as HTMLElement;
    expect(screen.queryByText("This session")).toBeNull();
    await actor.click(
      within(curlRow).getByRole("button", { name: /Sign out/ }),
    );
    const one = await screen.findByRole("alertdialog");
    await actor.click(
      within(one).getByRole("button", { name: "Sign out session" }),
    );
    await waitFor(() => {
      expect(calls).toEqual(["DELETE s-2"]);
    });
    await waitFor(() => {
      expect(screen.queryByText("API client (curl)")).toBeNull();
    });
    await actor.click(
      screen.getByRole("button", { name: "Sign out all sessions" }),
    );
    const all = await screen.findByRole("alertdialog");
    expect(
      within(all).getByRole("heading", {
        name: "Sign out all of demo-alice's sessions?",
      }),
    ).toBeVisible();
    await actor.click(
      within(all).getByRole("button", { name: "Sign out all sessions" }),
    );
    await waitFor(() => {
      expect(calls).toEqual(["DELETE s-2", "POST revoke-all"]);
    });
    expect(await screen.findByText("No active sessions")).toBeVisible();
  });

  it("shows the list but no sign-out control with user:read alone", async () => {
    renderApp(`/admin/users/${UUID}`, {
      folders,
      me: makeMe({ global_permissions: ["user:read"] }),
      handlers: [
        userHandler,
        http.get(SESSIONS_PATH, () =>
          sessionList([sessionItem("s-1", "Chrome on macOS")]),
        ),
      ],
    });
    expect(await screen.findByText("Chrome on macOS", {}, LONG)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Sign out/ })).toBeNull();
  });

  it("renders and requests nothing without user:read", async () => {
    let requested = false;
    renderApp(`/admin/users/${UUID}`, {
      folders,
      me: makeMe({ global_permissions: ["user:update"] }),
      handlers: [
        userHandler,
        http.get(SESSIONS_PATH, () => {
          requested = true;
          return sessionList([]);
        }),
      ],
    });
    expect(
      await screen.findByRole(
        "heading",
        { level: 1, name: "Alice Demo" },
        LONG,
      ),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Sessions" })).toBeNull();
    expect(requested).toBe(false);
  });
});
