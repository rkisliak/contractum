// The shared card list (CardIndex) on /cards and the folder Subscribers tab (FolderCards): loading,
// failure, paging, sorting, debounced search, where "Create card" is offered and where the card
// goes, the folder tab's search params, plus folderLabel, cardKeys and the "no body" guards in
// api.ts.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type {
  SubscriberCard,
  SubscriberCardIndex,
  SubscriberCardIndexItem,
} from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder, ROOT_ID } from "@/test/renderApp";
import { cardKeys } from "./api";
import { folderLabel } from "./CardIndex";

afterEach(() => {
  tokenStore.clear();
});

const acme = makeFolder({ id: "acme-id", display_name: "Acme", path: "acme" });
const payments = makeFolder({
  id: "payments-id",
  parent_id: "acme-id",
  kind: "project",
  display_name: "Payments",
  path: "acme/payments",
});
const ledger = makeFolder({
  id: "ledger-id",
  parent_id: "payments-id",
  kind: "team",
  display_name: "Ledger",
  path: "acme/payments/ledger",
});
const fraud = makeFolder({
  id: "fraud-id",
  parent_id: "acme-id",
  kind: "team",
  display_name: "Fraud",
  path: "acme/fraud",
});
const retired = makeFolder({
  id: "retired-id",
  parent_id: "acme-id",
  kind: "team",
  display_name: "Retired",
  path: "acme/retired",
  archived: true,
  archived_at: "2026-11-02T10:00:00Z",
});
const folders = [rootFolder, acme, payments, ledger, fraud, retired];
const creator = ["card:read", "card:create"];

function item(
  overrides: Partial<SubscriberCardIndexItem> = {},
): SubscriberCardIndexItem {
  return {
    id: "card-1",
    name: "alpha",
    description: "",
    folder_id: "ledger-id",
    folder_ancestor_ids: [],
    organization_id: "acme-id",
    archived_at: null,
    created_at: "2026-11-01T10:00:00Z",
    updated_at: "2026-11-02T10:00:00Z",
    active_subscription_count: 0,
    subscriptions: [],
    ...overrides,
  };
}

function page(
  items: SubscriberCardIndexItem[],
  extra: Partial<SubscriberCardIndex> = {},
): SubscriberCardIndex {
  return {
    items,
    next_cursor: null,
    total_cards: items.length,
    total_folders: new Set(items.map((row) => row.folder_id)).size,
    pin_preview_limit: 5,
    ...extra,
  };
}

function makeCard(overrides: Partial<SubscriberCard> = {}): SubscriberCard {
  return {
    id: "card-new",
    folder_id: "ledger-id",
    name: "ops",
    description: "Ops",
    contacts: [],
    email_enabled: false,
    webhook_enabled: false,
    webhook_url: null,
    webhook_secret_configured: false,
    archived_at: null,
    archived_by: null,
    created_by: "local:admin",
    created_at: "2026-11-01T10:00:00Z",
    updated_by: "local:admin",
    updated_at: "2026-11-01T10:00:00Z",
    members: [],
    subscriptions: [],
    subscriptions_truncated: false,
    ...overrides,
  };
}

function problem(status: number, title: string) {
  return HttpResponse.json(
    { type: "urn:dc:problem:conflict", title, status },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

function listHandler(
  seen: URLSearchParams[],
  answer: (query: URLSearchParams) => Response,
) {
  return http.get("*/api/v1/contracts/cards", ({ request }) => {
    const query = new URL(request.url).searchParams;
    seen.push(query);
    return answer(query);
  });
}

function cellTexts(table: HTMLElement): string[] {
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("link")[0]?.textContent ?? "");
}

describe("CardIndex states", () => {
  it("shows a skeleton while loading, then a failure with Retry loading", async () => {
    const user = userEvent.setup();
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let calls = 0;
    renderApp("/cards", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", async () => {
          calls += 1;
          if (calls === 1) {
            await gate;
            return problem(500, "Cards unavailable");
          }
          return HttpResponse.json(page([item()]));
        }),
      ],
    });
    expect(
      await screen.findByText("Loading subscriber cards"),
    ).toBeInTheDocument();
    release();
    expect(await screen.findByText("Cards unavailable")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("table", {
        name: "Subscriber cards in all folders",
      }),
    ).toBeVisible();
    expect(screen.getByText("1 card in 1 folder")).toBeVisible();
  });

  it("renders a generic problem when the list answers without a body", async () => {
    renderApp("/cards", {
      folders,
      permissions: creator,
      handlers: [
        http.get(
          "*/api/v1/contracts/cards",
          () => new HttpResponse(null, { status: 204 }),
        ),
      ],
    });
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });

  it("pages with the cursor, keeps rows after a failed page and sorts by column", async () => {
    const user = userEvent.setup();
    const seen: URLSearchParams[] = [];
    let failNext = true;
    renderApp("/cards", {
      folders,
      permissions: creator,
      handlers: [
        listHandler(seen, (query) => {
          if (query.get("cursor") === "c2") {
            if (failNext) {
              failNext = false;
              return problem(500, "Next page failed");
            }
            return HttpResponse.json(
              page([
                item({
                  id: "card-3",
                  name: "Charlie",
                  folder_id: "fraud-id",
                  active_subscription_count: 1,
                  updated_at: "2026-11-01T09:00:00Z",
                }),
              ]),
            );
          }
          return HttpResponse.json(
            page(
              [
                item({
                  id: "card-2",
                  name: "bravo",
                  folder_id: "fraud-id",
                  active_subscription_count: 5,
                  updated_at: "2026-11-04T10:00:00Z",
                  archived_at: "2026-11-05T10:00:00Z",
                }),
                item({ id: "card-1", name: "Alpha" }),
              ],
              { next_cursor: "c2", total_cards: 3, total_folders: 2 },
            ),
          );
        }),
      ],
    });
    const table = await screen.findByRole("table", {
      name: "Subscriber cards in all folders",
    });
    expect(screen.getByText("3 cards in 2 folders")).toBeVisible();
    expect(seen[0]?.get("limit")).toBe("50");
    expect(seen[0]?.get("include_archived")).toBe("false");
    expect(seen[0]?.has("cursor")).toBe(false);
    const bravoRow = within(table).getByRole("row", { name: /bravo/ });
    expect(within(bravoRow).getByText("Archived")).toBeVisible();
    expect(within(bravoRow).getByText("None")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(await screen.findByText("Next page failed")).toBeVisible();
    expect(seen.at(-1)?.get("cursor")).toBe("c2");
    expect(cellTexts(table)).toEqual(["bravo", "Alpha"]);
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(await within(table).findByText("Charlie")).toBeVisible();
    expect(screen.getByText("All results loaded")).toBeVisible();

    // Name sorts case-insensitively.
    await user.click(within(table).getByRole("button", { name: /^Card/ }));
    expect(cellTexts(table)).toEqual(["Alpha", "bravo", "Charlie"]);
    await user.click(
      within(table).getByRole("button", { name: /^Active subscriptions/ }),
    );
    expect(cellTexts(table)).toEqual(["Alpha", "Charlie", "bravo"]);
    await user.click(within(table).getByRole("button", { name: /^Updated/ }));
    expect(cellTexts(table)).toEqual(["Charlie", "Alpha", "bravo"]);
    // Folder path: "Acme / Fraud" before "Acme / Payments / Ledger".
    await user.click(within(table).getByRole("button", { name: /^Folder/ }));
    expect(cellTexts(table)[2]).toBe("Alpha");
  });

  it("sorts a card in a folder the caller cannot see first by folder", async () => {
    const user = userEvent.setup();
    renderApp("/cards", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", () =>
          HttpResponse.json(
            page([
              item({ id: "a", name: "seen" }),
              item({ id: "b", name: "hidden", folder_id: "nowhere" }),
            ]),
          ),
        ),
      ],
    });
    const table = await screen.findByRole("table", {
      name: "Subscriber cards in all folders",
    });
    await user.click(within(table).getByRole("button", { name: /^Folder/ }));
    expect(cellTexts(table)).toEqual(["hidden", "seen"]);
  });

  it("applies a typed search after the debounce without pressing Enter", async () => {
    const user = userEvent.setup();
    const seen: URLSearchParams[] = [];
    const { router } = renderApp("/cards", {
      folders,
      permissions: creator,
      handlers: [listHandler(seen, () => HttpResponse.json(page([item()])))],
    });
    await screen.findByRole("table", {
      name: "Subscriber cards in all folders",
    });
    await user.type(screen.getByLabelText("Search cards"), "  ledger ");
    await waitFor(() => {
      expect(seen.at(-1)?.get("q")).toBe("ledger");
    });
    expect(router.state.location.search).toEqual({ q: "ledger" });
    // The debounced value lands once, trimmed.
    expect(seen.filter((query) => query.has("q"))).toHaveLength(1);
  });
});

describe("CardIndex create gating and target folder", () => {
  it("offers no Create card when card:create is held nowhere", async () => {
    renderApp("/cards", {
      folders,
      permissions: ["card:read"],
      handlers: [
        http.get("*/api/v1/contracts/cards", () => HttpResponse.json(page([]))),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "No subscriber cards yet" }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Create card" })).toBeNull();
  });

  it("offers Create card in the toolbar once cards are listed", async () => {
    renderApp("/cards", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", () =>
          HttpResponse.json(page([item()])),
        ),
      ],
    });
    await screen.findByRole("table", {
      name: "Subscriber cards in all folders",
    });
    expect(screen.getAllByRole("button", { name: "Create card" })).toHaveLength(
      1,
    );
  });

  it("creates straight in a Project scope without subfolders", async () => {
    const user = userEvent.setup();
    let createdIn: unknown = null;
    const { router } = renderApp("/cards?folder=payments-id&subtree=false", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", () => HttpResponse.json(page([]))),
        http.post("*/api/v1/contracts/folders/:id/cards", ({ params }) => {
          createdIn = params.id;
          return HttpResponse.json(makeCard(), { status: 201 });
        }),
        http.get("*/api/v1/contracts/cards/:id", () =>
          HttpResponse.json(makeCard()),
        ),
      ],
    });
    expect(
      await screen.findByText(/no cards you can see in Acme \/ Payments\./),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Create card" }));
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    expect(within(dialog).queryByLabelText("Folder")).toBeNull();
    expect(
      within(dialog).getByText(
        "The card is created in Payments. Subscribe it to contract versions next.",
      ),
    ).toBeVisible();
    await user.type(within(dialog).getByLabelText("Name"), "ops");
    await user.type(within(dialog).getByLabelText("Description"), "Ops");
    await user.click(
      within(dialog).getByRole("button", { name: "Create card" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/cards/card-new");
    });
    expect(createdIn).toBe("payments-id");
  });

  it("offers the Project and its Teams as choices, the scope preselected, for a Project scope with subfolders", async () => {
    const user = userEvent.setup();
    renderApp("/cards?folder=payments-id", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", () => HttpResponse.json(page([]))),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create card" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    const options = within(within(dialog).getByLabelText("Folder"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(options).toEqual([
      "Choose a folder",
      "Acme / Payments",
      "Acme / Payments / Ledger",
    ]);
  });

  it("creates a card in an Organization folder, preselected from the Organization scope", async () => {
    const user = userEvent.setup();
    let createdIn: unknown = null;
    const { router } = renderApp("/folders/acme-id/cards", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", () => HttpResponse.json(page([]))),
        http.post("*/api/v1/contracts/folders/:id/cards", ({ params }) => {
          createdIn = params.id;
          return HttpResponse.json(makeCard({ folder_id: "acme-id" }), {
            status: 201,
          });
        }),
        http.get("*/api/v1/contracts/cards/:id", () =>
          HttpResponse.json(makeCard({ folder_id: "acme-id" })),
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create card" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    expect(within(dialog).getByLabelText("Folder")).toHaveDisplayValue("Acme");
    await user.type(within(dialog).getByLabelText("Name"), "org-consumers");
    await user.type(within(dialog).getByLabelText("Description"), "Org level");
    await user.click(
      within(dialog).getByRole("button", { name: "Create card" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/cards/card-new");
    });
    expect(createdIn).toBe("acme-id");
  });

  it("creates a card at Root from the all-folders scope", async () => {
    const user = userEvent.setup();
    let createdIn: unknown = null;
    const { router } = renderApp("/cards", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", () => HttpResponse.json(page([]))),
        http.post("*/api/v1/contracts/folders/:id/cards", ({ params }) => {
          createdIn = params.id;
          return HttpResponse.json(makeCard({ folder_id: ROOT_ID }), {
            status: 201,
          });
        }),
        http.get("*/api/v1/contracts/cards/:id", () =>
          HttpResponse.json(makeCard({ folder_id: ROOT_ID })),
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create card" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    await user.selectOptions(within(dialog).getByLabelText("Folder"), "ROOT");
    await user.type(within(dialog).getByLabelText("Name"), "platform-wide");
    await user.type(within(dialog).getByLabelText("Description"), "Everyone");
    await user.click(
      within(dialog).getByRole("button", { name: "Create card" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/cards/card-new");
    });
    expect(createdIn).toBe(ROOT_ID);
  });

  it("lists a Root-owned card with a null Organization", async () => {
    renderApp("/cards", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", () =>
          HttpResponse.json(
            page([
              item({
                id: "card-root",
                name: "platform-wide",
                folder_id: ROOT_ID,
                folder_ancestor_ids: [ROOT_ID],
                organization_id: null,
              }),
            ]),
          ),
        ),
      ],
    });
    const table = await screen.findByRole("table", {
      name: "Subscriber cards in all folders",
    });
    expect(
      within(table).getByRole("link", { name: "platform-wide" }),
    ).toBeVisible();
    expect(within(table).getByRole("link", { name: "ROOT" })).toBeVisible();
  });

  it("offers every active folder, Root first then sorted, for the all-folders scope", async () => {
    const user = userEvent.setup();
    renderApp("/cards", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", () => HttpResponse.json(page([]))),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create card" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    const options = within(within(dialog).getByLabelText("Folder"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    // The archived Team is never offered; every other kind is, Root first.
    expect(options).toEqual([
      "Choose a folder",
      "ROOT",
      "Acme",
      "Acme / Fraud",
      "Acme / Payments",
      "Acme / Payments / Ledger",
    ]);
  });

  it("offers no Create card in an archived Team", async () => {
    renderApp("/folders/retired-id/cards", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", () => HttpResponse.json(page([]))),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "No subscriber cards yet" }),
    ).toBeVisible();
    expect(screen.getByText(/no cards you can see in Retired\./)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Create card" })).toBeNull();
  });

  it("offers no Create card in a Team where card:create is not held", async () => {
    renderApp("/folders/fraud-id/cards", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/me/capabilities", ({ request }) => {
          const folderId = new URL(request.url).searchParams.get("folder_id");
          return HttpResponse.json({
            folder_id: folderId,
            roles: [],
            permissions:
              folderId === ROOT_ID
                ? ["card:read", "card:create"]
                : ["card:read"],
            inheritance_blocked: false,
            grantable_roles: [],
          });
        }),
        http.get("*/api/v1/contracts/cards", () => HttpResponse.json(page([]))),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "No subscriber cards yet" }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Create card" })).toBeNull();
  });
});

describe("FolderCards", () => {
  it("keeps q, archived and active in the folder tab's search params", async () => {
    const user = userEvent.setup();
    const seen: URLSearchParams[] = [];
    const { router } = renderApp("/folders/payments-id/cards", {
      folders,
      permissions: creator,
      handlers: [
        listHandler(seen, (query) =>
          HttpResponse.json(page(query.get("q") === "none" ? [] : [item()])),
        ),
      ],
    });
    await screen.findByRole("table", { name: "Subscriber cards in Payments" });
    expect(seen[0]?.get("folder_id")).toBe("payments-id");
    expect(seen[0]?.get("subtree")).toBe("true");
    expect(router.state.location.search).toEqual({});

    await user.click(screen.getByLabelText("Show archived"));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ archived: true });
    });
    await user.click(screen.getByLabelText("Only with active subscriptions"));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        archived: true,
        active: true,
      });
    });
    await user.type(screen.getByLabelText("Search cards"), "none{Enter}");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        q: "none",
        archived: true,
        active: true,
      });
    });
    expect(seen.at(-1)?.get("q")).toBe("none");
    expect(seen.at(-1)?.get("has_active_subscriptions")).toBe("true");
    expect(seen.at(-1)?.get("include_archived")).toBe("true");
    expect(
      await screen.findByRole("heading", {
        name: "No cards match these filters",
      }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ archived: true });
    });
    expect(screen.getByLabelText("Search cards")).toHaveValue("");
  });

  it("starts from the folder tab's search params", async () => {
    const seen: URLSearchParams[] = [];
    renderApp("/folders/fraud-id/cards?q=ops&archived=true&active=true", {
      folders,
      permissions: creator,
      handlers: [listHandler(seen, () => HttpResponse.json(page([item()])))],
    });
    await screen.findByRole("table", { name: "Subscriber cards in Fraud" });
    expect(seen[0]?.get("q")).toBe("ops");
    expect(seen[0]?.get("include_archived")).toBe("true");
    expect(seen[0]?.get("has_active_subscriptions")).toBe("true");
    expect(screen.getByLabelText("Search cards")).toHaveValue("ops");
    expect(screen.getByLabelText("Show archived")).toBeChecked();
    expect(
      screen.getByLabelText("Only with active subscriptions"),
    ).toBeChecked();
  });
});

describe("folderLabel", () => {
  const byId = new Map(folders.map((folder) => [folder.id, folder]));

  it("joins the readable ancestors below Root", () => {
    expect(folderLabel(ledger, byId)).toBe("Acme / Payments / Ledger");
  });

  it("stops at a parent the caller cannot read", () => {
    expect(folderLabel(ledger, new Map([[ledger.id, ledger]]))).toBe("Ledger");
  });

  it("falls back to the display name for Root itself", () => {
    expect(folderLabel(rootFolder, byId)).toBe("ROOT");
  });

  it("stops after ten levels on a parent cycle", () => {
    const a = makeFolder({ id: "a", parent_id: "b", display_name: "A" });
    const b = makeFolder({ id: "b", parent_id: "a", display_name: "B" });
    const cycle = new Map([
      ["a", a],
      ["b", b],
    ]);
    expect(folderLabel(a, cycle).split(" / ")).toHaveLength(10);
  });
});

describe("cardKeys", () => {
  it("roots every key under cards so one invalidation refreshes them all", () => {
    const keys = [
      cardKeys.all,
      cardKeys.folder("f-1", true),
      cardKeys.index({ q: "x" }),
      cardKeys.detail("c-1"),
      cardKeys.subscriptions("c-1", "active"),
      cardKeys.notices("c-1"),
    ];
    for (const key of keys) expect(key[0]).toBe("cards");
    expect(cardKeys.folder("f-1", true)).toEqual([
      "cards",
      "folder",
      "f-1",
      { includeArchived: true },
    ]);
    expect(cardKeys.subscriptions("c-1", "ended")).toEqual([
      "cards",
      "subscriptions",
      "c-1",
      "ended",
    ]);
  });
});

describe("api.ts body guards", () => {
  const empty = () => new HttpResponse(null, { status: 204 });

  it("renders a generic problem when the card answers without a body", async () => {
    renderApp("/cards/card-1", {
      folders,
      permissions: creator,
      handlers: [http.get("*/api/v1/contracts/cards/:id", empty)],
    });
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });

  it("shows a generic problem in the dialog when create answers without a body", async () => {
    const user = userEvent.setup();
    renderApp("/folders/ledger-id/cards", {
      folders,
      permissions: creator,
      handlers: [
        http.get("*/api/v1/contracts/cards", () => HttpResponse.json(page([]))),
        http.post("*/api/v1/contracts/folders/:id/cards", empty),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create card" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    await user.type(within(dialog).getByLabelText("Name"), "ops");
    await user.type(within(dialog).getByLabelText("Description"), "Ops");
    await user.click(
      within(dialog).getByRole("button", { name: "Create card" }),
    );
    expect(
      await within(dialog).findByText("Something went wrong"),
    ).toBeVisible();
  });

  it("shows a generic problem when the card update answers without a body", async () => {
    const user = userEvent.setup();
    renderApp("/cards/card-new", {
      folders,
      permissions: ["card:read", "card:manage"],
      handlers: [
        http.get("*/api/v1/contracts/cards/:id", () =>
          HttpResponse.json(makeCard()),
        ),
        http.patch("*/api/v1/contracts/cards/:id", empty),
      ],
    });
    await user.click(await screen.findByRole("button", { name: "Edit card" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit card" });
    await user.click(within(dialog).getByRole("button", { name: "Save card" }));
    expect(
      await within(dialog).findByText("Something went wrong"),
    ).toBeVisible();
  });

  it("shows a generic problem when rotation answers without a body", async () => {
    const user = userEvent.setup();
    renderApp("/cards/card-new?tab=webhook", {
      folders,
      permissions: ["card:read", "card:manage"],
      handlers: [
        http.get("*/api/v1/contracts/cards/:id", () =>
          HttpResponse.json(makeCard()),
        ),
        http.post("*/api/v1/contracts/cards/:id/webhook-secret", empty),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Rotate secret" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Rotate the webhook secret?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Rotate secret" }),
    );
    expect(
      await within(confirm).findByText("Something went wrong"),
    ).toBeVisible();
  });

  it("shows a generic problem when notices answer without a body", async () => {
    renderApp("/cards/card-new?tab=notices", {
      folders,
      permissions: ["card:read"],
      handlers: [
        http.get("*/api/v1/contracts/cards/:id", () =>
          HttpResponse.json(makeCard()),
        ),
        http.get("*/api/v1/contracts/cards/:id/notices", empty),
      ],
    });
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });
});
