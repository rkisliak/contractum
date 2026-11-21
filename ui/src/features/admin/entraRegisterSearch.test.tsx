// The Register Entra dialogs search the directory (quick 261119-ofa, 261119-r5g): debounced, 2+
// characters, a dropdown (combobox + listbox) under the input with display name, secondary line and
// object ID, already-registered options disabled, an empty state, a truncation note, a warning when the
// directory is unreachable, and the object-ID fallback. Registration still posts to the unchanged routes.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { delay } from "msw";
import type { EntraDirectoryMatch, Me } from "@/api/types";
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

const LOAD = { timeout: 10_000 };
const TID = "11111111-2222-4333-8444-555555555555";
const ALICE = "0b5c3f8e-1a2b-4c3d-8e9f-0123456789ab";
const ALICIA = "9a8b7c6d-1a2b-4c3d-8e9f-0123456789ab";
const SEARCH = "*/api/v1/identity/entra/directory/search";
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

const page = (items: unknown[]) =>
  HttpResponse.json({ items, next_cursor: null });

function match(
  overrides: Partial<EntraDirectoryMatch> = {},
): EntraDirectoryMatch {
  return {
    object_id: ALICE,
    tenant_id: TID,
    display_name: "Alice Anders",
    secondary: "alice@contoso.test",
    kind: "user",
    already_registered: false,
    ...overrides,
  };
}

function problem(status: number, slug: string) {
  return HttpResponse.json(
    { type: `urn:dc:problem:${slug}`, title: "Refused", status, detail: "No." },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

async function openDialog(
  actor: ReturnType<typeof userEvent.setup>,
  entry: string,
) {
  await actor.click(await screen.findByRole("button", { name: "New" }, LOAD));
  await actor.click(await screen.findByRole("menuitem", { name: entry }));
  return screen.findByRole("dialog", { name: entry });
}

function usersPage(extra: RequestHandler[] = []) {
  return renderApp("/admin/users?source=entra", {
    folders,
    ...ENTRA,
    me: entraAdmin,
    handlers: [
      http.get("*/api/v1/identity/users", () => page([])),
      http.get("*/api/v1/identity/entra/users", () => page([])),
      ...extra,
    ],
  });
}

describe("the Register Entra user dialog searches the directory", () => {
  it("sends nothing for fewer than 2 characters and shows the helper", async () => {
    const actor = userEvent.setup();
    const queries: string[] = [];
    usersPage([
      http.get(SEARCH, ({ request }) => {
        queries.push(new URL(request.url).search);
        return HttpResponse.json({ items: [], truncated: false });
      }),
    ]);
    const dialog = await openDialog(actor, "Register Entra user");
    const box = within(dialog).getByLabelText("Search Entra users");
    expect(
      within(dialog).getByText(
        "Name, email or user principal name (at least 2 characters), or paste an object ID.",
      ),
    ).toBeVisible();
    await actor.type(box, "a");
    await delay(500);
    expect(queries).toEqual([]);
  });

  it("sends one request after the pause and lists name, secondary line and object ID", async () => {
    const actor = userEvent.setup();
    const queries: URLSearchParams[] = [];
    usersPage([
      http.get(SEARCH, ({ request }) => {
        queries.push(new URL(request.url).searchParams);
        return HttpResponse.json({
          items: [
            match(),
            match({
              object_id: ALICIA,
              display_name: "Alicia Brandt",
              secondary: null,
            }),
          ],
          truncated: false,
        });
      }),
    ]);
    const dialog = await openDialog(actor, "Register Entra user");
    await actor.type(
      within(dialog).getByLabelText("Search Entra users"),
      "ali",
    );
    const list = await within(dialog).findByRole(
      "listbox",
      { name: "Directory matches" },
      LOAD,
    );
    expect(within(list).getAllByRole("option")).toHaveLength(2);
    const alice = within(list).getByRole("option", { name: /^Alice Anders/ });
    expect(alice).not.toHaveAttribute("aria-disabled");
    expect(within(list).getByText("alice@contoso.test")).toBeVisible();
    expect(within(list).getByText(ALICE)).toBeVisible();
    expect(within(list).getByText(ALICIA)).toBeVisible();
    // The list hangs off the input: the input points at it and is expanded.
    const input = within(dialog).getByRole("combobox", {
      name: "Search Entra users",
    });
    expect(input).toHaveAttribute("aria-expanded", "true");
    expect(input).toHaveAttribute("aria-controls", list.id);
    expect(queries).toHaveLength(1);
    expect(queries[0]?.get("kind")).toBe("user");
    expect(queries[0]?.get("q")).toBe("ali");
    expect(queries[0]?.has("owner_folder_id")).toBe(false);
  });

  it("disables an already registered match with a note", async () => {
    const actor = userEvent.setup();
    usersPage([
      http.get(SEARCH, () =>
        HttpResponse.json({
          items: [match({ already_registered: true })],
          truncated: false,
        }),
      ),
    ]);
    const dialog = await openDialog(actor, "Register Entra user");
    await actor.type(
      within(dialog).getByLabelText("Search Entra users"),
      "ali",
    );
    const option = await within(dialog).findByRole(
      "option",
      { name: /^Alice Anders/ },
      LOAD,
    );
    expect(option).toHaveAttribute("aria-disabled", "true");
    expect(option).toHaveAccessibleName(
      `Alice Anders alice@contoso.test ${ALICE} Already registered`,
    );
    // A disabled option cannot be picked.
    await actor.click(option);
    expect(
      within(dialog).getByRole("combobox", { name: "Search Entra users" }),
    ).toHaveValue("ali");
    expect(within(dialog).getByRole("listbox")).toBeVisible();
  });

  it("says when nothing matches and when the list is truncated", async () => {
    const actor = userEvent.setup();
    let truncated = false;
    usersPage([
      http.get(SEARCH, () =>
        HttpResponse.json({
          items: truncated ? [match()] : [],
          truncated,
        }),
      ),
    ]);
    const dialog = await openDialog(actor, "Register Entra user");
    const box = within(dialog).getByLabelText("Search Entra users");
    await actor.type(box, "zzz");
    expect(
      await within(dialog).findByText(
        "No matches in the directory. Check the spelling, or enter an object ID instead.",
        {},
        LOAD,
      ),
    ).toBeVisible();
    truncated = true;
    await actor.clear(box);
    await actor.type(box, "many");
    expect(
      await within(dialog).findByText(
        "Showing the first 20 matches. Type more to narrow the list.",
        {},
        LOAD,
      ),
    ).toBeVisible();
  });

  it("registers the picked match with its object ID and tenant", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    usersPage([
      http.get(SEARCH, () =>
        HttpResponse.json({ items: [match()], truncated: false }),
      ),
      http.post("*/api/v1/identity/entra/users", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(
          {
            id: "eu-1",
            principal_id: `entra:${TID}:${ALICE}`,
            tenant_id: TID,
            object_id: ALICE,
            display_name: "Alice Anders",
            preferred_username: null,
            is_active: true,
            registered_directly: true,
            ...AUDIT,
          },
          { status: 201 },
        );
      }),
    ]);
    const dialog = await openDialog(actor, "Register Entra user");
    const submit = within(dialog).getByRole("button", {
      name: "Register Entra user",
    });
    // Nothing picked yet.
    await actor.click(submit);
    expect(
      within(dialog).getByText(
        "Choose a user from the results, or enter an object ID instead.",
      ),
    ).toBeVisible();
    expect(bodies).toEqual([]);
    await actor.type(
      within(dialog).getByLabelText("Search Entra users"),
      "ali",
    );
    await actor.click(
      await within(dialog).findByRole(
        "option",
        { name: /^Alice Anders/ },
        LOAD,
      ),
    );
    // The list closes; the box holds the name and the helper line the email and object ID.
    expect(within(dialog).queryByRole("listbox")).toBeNull();
    expect(
      within(dialog).getByRole("combobox", { name: "Search Entra users" }),
    ).toHaveValue("Alice Anders");
    expect(
      within(dialog).getByText(`alice@contoso.test · ${ALICE}`),
    ).toBeVisible();
    await actor.click(submit);
    await waitFor(() => {
      expect(bodies).toEqual([{ object_id: ALICE, tenant_id: TID }]);
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("works from the keyboard: arrows and Enter pick, Escape closes only the list, clear resets", async () => {
    const actor = userEvent.setup();
    usersPage([
      http.get(SEARCH, () =>
        HttpResponse.json({
          items: [
            match({ already_registered: true }),
            match({
              object_id: ALICIA,
              display_name: "Alicia Brandt",
              secondary: null,
            }),
          ],
          truncated: false,
        }),
      ),
    ]);
    const dialog = await openDialog(actor, "Register Entra user");
    const input = within(dialog).getByRole("combobox", {
      name: "Search Entra users",
    });
    await actor.type(input, "ali");
    await within(dialog).findByRole("listbox", {}, LOAD);
    // Escape closes the list, not the dialog.
    await actor.keyboard("{Escape}");
    expect(within(dialog).queryByRole("listbox")).toBeNull();
    expect(screen.getByRole("dialog", { name: "Register Entra user" })).toBe(
      dialog,
    );
    // Typing reopens it; the first option is disabled, so ArrowDown lands on the second.
    await actor.type(input, "c");
    const options = await within(dialog).findAllByRole("option", {}, LOAD);
    await actor.keyboard("{ArrowDown}");
    expect(input).toHaveAttribute("aria-activedescendant", options[1]?.id);
    await actor.keyboard("{Enter}");
    expect(within(dialog).queryByRole("listbox")).toBeNull();
    expect(input).toHaveValue("Alicia Brandt");
    expect(within(dialog).getByText(ALICIA)).toBeVisible();
    // Typing again forgets the pick; the clear button empties the box.
    await actor.type(input, "t");
    expect(within(dialog).queryByText(ALICIA, { selector: "p" })).toBeNull();
    await actor.click(
      within(dialog).getByRole("button", { name: "Clear search" }),
    );
    expect(input).toHaveValue("");
    expect(input).toHaveFocus();
  });

  it("warns when the directory is unreachable and still registers by pasted object ID", async () => {
    const actor = userEvent.setup();
    const bodies: unknown[] = [];
    usersPage([
      http.get(SEARCH, () => problem(503, "service-unavailable")),
      http.post("*/api/v1/identity/entra/users", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(
          {
            id: "eu-1",
            principal_id: `entra:${TID}:${ALICE}`,
            tenant_id: TID,
            object_id: ALICE,
            display_name: "Alice Anders",
            preferred_username: null,
            is_active: true,
            registered_directly: true,
            ...AUDIT,
          },
          { status: 201 },
        );
      }),
    ]);
    const dialog = await openDialog(actor, "Register Entra user");
    await actor.type(
      within(dialog).getByLabelText("Search Entra users"),
      "ali",
    );
    expect(
      await within(dialog).findByText(
        /The Entra directory is unreachable or not configured, so it cannot be searched/,
        {},
        LOAD,
      ),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Retry search" }),
    ).toBeVisible();
    await actor.click(
      within(dialog).getByRole("button", {
        name: "Enter an object ID instead",
      }),
    );
    await actor.type(within(dialog).getByLabelText("Object ID"), ALICE);
    await actor.click(
      within(dialog).getByRole("button", { name: "Register Entra user" }),
    );
    await waitFor(() => {
      expect(bodies).toEqual([{ object_id: ALICE, tenant_id: null }]);
    });
  });
});

describe("the Register Entra group dialog", () => {
  it("searches under the chosen owner and registers the pick there", async () => {
    const actor = userEvent.setup();
    const queries: URLSearchParams[] = [];
    const bodies: unknown[] = [];
    renderApp("/admin/groups?source=entra", {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/groups", () => page([])),
        http.get("*/api/v1/identity/entra/groups", () => page([])),
        http.get(SEARCH, ({ request }) => {
          queries.push(new URL(request.url).searchParams);
          return HttpResponse.json({
            items: [
              match({
                kind: "group",
                display_name: "Data Eng",
                secondary: "dataeng@contoso.test",
              }),
            ],
            truncated: false,
          });
        }),
        http.post("*/api/v1/identity/entra/groups", async ({ request }) => {
          bodies.push(await request.json());
          return HttpResponse.json(
            {
              id: "eg-1",
              tenant_id: TID,
              object_id: ALICE,
              owner_folder_id: "acme-id",
              display_name: "Data Eng",
              ...AUDIT,
            },
            { status: 201 },
          );
        }),
      ],
    });
    const dialog = await openDialog(actor, "Register Entra group");
    // The owner comes first, the search depends on it.
    const fields = within(dialog)
      .getAllByRole("combobox")
      .map((field) => field.id);
    expect(fields).toEqual(["register-group-owner", "register-group-search"]);
    await actor.selectOptions(
      within(dialog).getByLabelText("Owner"),
      "acme-id",
    );
    await actor.type(
      within(dialog).getByLabelText("Search Entra groups"),
      "data",
    );
    await actor.click(
      await within(dialog).findByRole("option", { name: /^Data Eng/ }, LOAD),
    );
    expect(queries).toHaveLength(1);
    expect(queries[0]?.get("kind")).toBe("group");
    expect(queries[0]?.get("owner_folder_id")).toBe("acme-id");
    await actor.click(
      within(dialog).getByRole("button", { name: "Register Entra group" }),
    );
    await waitFor(() => {
      expect(bodies).toEqual([
        { object_id: ALICE, tenant_id: TID, owner_folder_id: "acme-id" },
      ]);
    });
    expect(ROOT_ID).not.toBe("acme-id");
  });
});

describe("the merged lists have one New button when empty", () => {
  it.each([
    ["/admin/users", "No users yet"],
    ["/admin/groups", "No groups yet"],
    ["/admin/service-accounts", "No service accounts yet"],
  ])("%s", async (path, heading) => {
    renderApp(path, {
      folders,
      ...ENTRA,
      me: entraAdmin,
      handlers: [
        http.get("*/api/v1/identity/users", () => page([])),
        http.get("*/api/v1/identity/entra/users", () => page([])),
        http.get("*/api/v1/identity/groups", () => page([])),
        http.get("*/api/v1/identity/entra/groups", () => page([])),
        http.get("*/api/v1/identity/service-accounts", () => page([])),
        http.get("*/api/v1/identity/entra/service-principals", () => page([])),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: heading }, LOAD),
    ).toBeVisible();
    expect(screen.getAllByRole("button", { name: "New" })).toHaveLength(1);
  });
});
