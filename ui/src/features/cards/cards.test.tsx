// Subscriber cards through the real router: the Subscriptions page and folder tab list, creating
// a card, the card page (edit, archive), members, subscribe / move / unsubscribe, notices and
// webhook secret rotation shown once.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type {
  CardSubscription,
  SubscriberCard,
  SubscriberCardIndex,
  SubscriberCardIndexItem,
} from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder } from "@/test/renderApp";

afterEach(() => {
  tokenStore.clear();
});

const acme = makeFolder({ id: "acme-id", display_name: "Acme", path: "acme" });
const fraud = makeFolder({
  id: "fraud-id",
  parent_id: "acme-id",
  kind: "team",
  slug: "fraud",
  display_name: "Fraud",
  path: "acme/fraud",
});
const folders = [rootFolder, acme, fraud];

const cardPermissions = [
  "card:read",
  "card:create",
  "card:write",
  "card:manage",
  "card:manage-secret",
];

function makeCard(overrides: Partial<SubscriberCard> = {}): SubscriberCard {
  return {
    id: "card-1",
    folder_id: "fraud-id",
    name: "demo-analytics-team",
    description: "Analytics team",
    contacts: [{ kind: "email", name: "On-call", value: "a@example.com" }],
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

const subscription: CardSubscription = {
  id: "sub-1",
  card_id: "card-1",
  contract_id: "contract-1",
  version_id: "v-1",
  semver: "1.0.0",
  mode: "pinned",
  pinned_by: "local:admin",
  pinned_at: "2026-11-02T10:00:00Z",
  ended_by: null,
  ended_at: null,
  end_reason: null,
  end_note: null,
  replaced_by_id: null,
};

function version(semver: string, id: string, extra: object = {}) {
  return {
    version_id: id,
    contract_id: "contract-1",
    semver,
    lifecycle: "published",
    status: "older",
    deprecated: false,
    deprecated_at: null,
    sunset_date: null,
    content_sha256: "x",
    engine_version: null,
    etag: "e",
    fetch_url: "/x",
    media_type: "application/yaml",
    odcs_api_version: "v3.1.0",
    pinned_rule_sets: null,
    published_at: "2026-11-01T10:00:00Z",
    published_by: "local:admin",
    schema_sha: null,
    size_bytes: 1,
    validation_run_id: null,
    ...extra,
  };
}

const contractHandlers = [
  http.get("*/api/v1/contracts/contracts/:id/versions", () =>
    HttpResponse.json({
      items: [
        version("1.1.0", "v-2", { status: "latest" }),
        version("1.0.0", "v-1", {
          deprecated: true,
          sunset_date: "2027-01-01",
        }),
      ],
    }),
  ),
  http.get("*/api/v1/contracts/contracts/:id", () =>
    HttpResponse.json({
      id: "contract-1",
      name: "Orders",
      odcs_id: "urn:orders",
    }),
  ),
];

function cardHandlers(card: SubscriberCard, subs: CardSubscription[] = []) {
  return [
    http.get("*/api/v1/contracts/cards/:id", () => HttpResponse.json(card)),
    http.get("*/api/v1/contracts/cards/:id/subscriptions", () =>
      HttpResponse.json({ items: subs, next_cursor: null }),
    ),
    http.get("*/api/v1/contracts/cards/:id/notices", () =>
      HttpResponse.json({
        items: [
          {
            id: "n-1",
            contract_id: "contract-1",
            version_id: "v-1",
            event_type: "version.deprecated",
            occurred_at: "2026-11-03T10:00:00Z",
            created_at: "2026-11-03T10:00:00Z",
            payload: { semver: "1.0.0" },
            deliveries: [
              {
                channel: "email",
                status: "delivered",
                attempts: 1,
                delivered_at: "2026-11-03T10:00:01Z",
                last_attempt_at: null,
                last_error: null,
                last_status: null,
              },
            ],
          },
        ],
        next_cursor: null,
      }),
    ),
    ...contractHandlers,
    // The "Pinned by" column names principals from the identity lists (usePrincipalNames).
    ...[
      "users",
      "groups",
      "service-accounts",
      "entra/users",
      "entra/groups",
      "entra/service-principals",
    ].map((list) =>
      http.get(`*/api/v1/identity/${list}`, () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    ),
  ];
}

function indexItem(
  overrides: Partial<SubscriberCardIndexItem> = {},
): SubscriberCardIndexItem {
  return {
    id: "card-1",
    name: "demo-analytics-team",
    description: "Analytics team",
    folder_id: "fraud-id",
    folder_ancestor_ids: ["root-id", "acme-id", "fraud-id"],
    organization_id: "acme-id",
    archived_at: null,
    created_at: "2026-11-01T10:00:00Z",
    updated_at: "2026-11-02T10:00:00Z",
    active_subscription_count: 7,
    subscriptions: [
      {
        contract_id: "contract-1",
        version_id: "v-1",
        semver: "1.0.0",
        contract_name: "Orders",
      },
      {
        contract_id: "contract-2",
        version_id: "v-9",
        semver: "2.0.0",
        contract_name: null,
      },
    ],
    ...overrides,
  };
}

function indexPage(items: SubscriberCardIndexItem[]): SubscriberCardIndex {
  return {
    items,
    next_cursor: null,
    total_cards: items.length,
    total_folders: new Set(items.map((item) => item.folder_id)).size,
    pin_preview_limit: 5,
  };
}

function indexHandler(
  seen: URLSearchParams[],
  items: SubscriberCardIndexItem[],
) {
  return http.get("*/api/v1/contracts/cards", ({ request }) => {
    seen.push(new URL(request.url).searchParams);
    return HttpResponse.json(indexPage(items));
  });
}

describe("subscriber cards", () => {
  it("lists every visible card on the Subscriptions page and creates one in a chosen folder", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    let createdIn: unknown = null;
    const seen: URLSearchParams[] = [];
    const { router } = renderApp("/cards", {
      folders,
      permissions: cardPermissions,
      handlers: [
        indexHandler(seen, [
          indexItem(),
          indexItem({
            id: "card-3",
            name: "unknown-folder-card",
            folder_id: "hidden-id",
            active_subscription_count: 0,
            subscriptions: [],
          }),
        ]),
        http.post(
          "*/api/v1/contracts/folders/:id/cards",
          async ({ request, params }) => {
            createdIn = params.id;
            body = await request.json();
            return HttpResponse.json(makeCard({ id: "card-2" }), {
              status: 201,
            });
          },
        ),
        ...cardHandlers(makeCard({ id: "card-2" })),
      ],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Subscriptions" }),
    ).toBeVisible();
    const table = await screen.findByRole("table", {
      name: "Subscriber cards in all folders",
    });
    expect(seen[0]?.has("folder_id")).toBe(false);
    expect(screen.getByText("2 cards in 2 folders")).toBeVisible();
    expect(
      within(table).getByRole("link", { name: "demo-analytics-team" }),
    ).toHaveAttribute("href", "/cards/card-1");
    expect(
      within(table).getByRole("link", { name: "Acme / Fraud" }),
    ).toHaveAttribute("href", "/folders/fraud-id");
    expect(within(table).getByText("Orders 1.0.0")).toBeVisible();
    expect(within(table).getByText("Restricted contract 2.0.0")).toBeVisible();
    expect(within(table).getByText("+5 more")).toBeVisible();
    expect(within(table).getByText("Folder not visible to you")).toBeVisible();
    expect(screen.getByLabelText("Scope")).toHaveDisplayValue(
      "All folders (Root)",
    );

    await user.click(screen.getByRole("button", { name: "Create card" }));
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    await user.type(within(dialog).getByLabelText("Name"), "ops");
    await user.type(within(dialog).getByLabelText("Description"), "Ops team");
    await user.click(
      within(dialog).getByRole("button", { name: "Create card" }),
    );
    expect(
      await within(dialog).findByText("Choose the folder the card belongs to."),
    ).toBeVisible();
    await user.selectOptions(
      within(dialog).getByLabelText("Folder"),
      "Acme / Fraud",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Add contact" }),
    );
    await user.type(within(dialog).getByLabelText("Value"), "ops@example.com");
    await user.click(
      within(dialog).getByRole("button", { name: "Create card" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/cards/card-2");
    });
    expect(createdIn).toBe("fraud-id");
    expect(body).toEqual({
      name: "ops",
      description: "Ops team",
      contacts: [{ kind: "email", value: "ops@example.com" }],
      email_enabled: false,
    });
  });

  it("switches the scope to a folder with or without its subfolders", async () => {
    const user = userEvent.setup();
    const seen: URLSearchParams[] = [];
    const { router } = renderApp("/cards", {
      folders,
      permissions: cardPermissions,
      handlers: [indexHandler(seen, [indexItem()])],
    });
    await screen.findByRole("table", {
      name: "Subscriber cards in all folders",
    });
    expect(screen.getByLabelText("Include subfolders")).toBeDisabled();

    await user.selectOptions(
      screen.getByLabelText("Scope"),
      "Acme (Organization)",
    );
    await screen.findByRole("table", { name: "Subscriber cards in Acme" });
    expect(router.state.location.search).toEqual({ folder: "acme-id" });
    expect(seen.at(-1)?.get("folder_id")).toBe("acme-id");
    expect(seen.at(-1)?.get("subtree")).toBe("true");

    await user.click(screen.getByLabelText("Include subfolders"));
    await waitFor(() => {
      expect(seen.at(-1)?.get("subtree")).toBe("false");
    });
    expect(router.state.location.search).toEqual({
      folder: "acme-id",
      subtree: false,
    });
  });

  it("applies search, archived and active-subscription filters", async () => {
    const user = userEvent.setup();
    const seen: URLSearchParams[] = [];
    renderApp("/cards", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.get("*/api/v1/contracts/cards", ({ request }) => {
          const query = new URL(request.url).searchParams;
          seen.push(query);
          return HttpResponse.json(
            indexPage(query.get("q") === "nothing" ? [] : [indexItem()]),
          );
        }),
      ],
    });
    await screen.findByRole("table", {
      name: "Subscriber cards in all folders",
    });
    expect(seen[0]?.get("include_archived")).toBe("false");

    await user.click(screen.getByLabelText("Show archived"));
    await waitFor(() => {
      expect(seen.at(-1)?.get("include_archived")).toBe("true");
    });
    await user.click(screen.getByLabelText("Only with active subscriptions"));
    await waitFor(() => {
      expect(seen.at(-1)?.get("has_active_subscriptions")).toBe("true");
    });
    await user.type(screen.getByLabelText("Search cards"), "nothing{Enter}");
    expect(
      await screen.findByRole("heading", {
        name: "No cards match these filters",
      }),
    ).toBeVisible();
    expect(seen.at(-1)?.get("q")).toBe("nothing");

    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await screen.findByRole("table", {
      name: "Subscriber cards in all folders",
    });
    expect(seen.at(-1)?.has("q")).toBe(false);
    expect(seen.at(-1)?.has("has_active_subscriptions")).toBe(false);
    expect(seen.at(-1)?.get("include_archived")).toBe("true");
  });

  it("shows the empty state only when no card exists in scope", async () => {
    renderApp("/cards", {
      folders,
      permissions: cardPermissions,
      handlers: [indexHandler([], [])],
    });
    expect(
      await screen.findByRole("heading", { name: "No subscriber cards yet" }),
    ).toBeVisible();
    expect(
      screen.getByText(/no cards you can see in all folders/),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Create card" })).toBeVisible();
  });

  it("lists an Organization's team cards on its Subscribers tab", async () => {
    const seen: URLSearchParams[] = [];
    renderApp("/folders/acme-id/cards", {
      folders,
      permissions: ["card:read"],
      handlers: [indexHandler(seen, [indexItem()])],
    });
    const table = await screen.findByRole("table", {
      name: "Subscriber cards in Acme",
    });
    expect(seen[0]?.get("folder_id")).toBe("acme-id");
    expect(seen[0]?.get("subtree")).toBe("true");
    expect(
      within(table).getByRole("link", { name: "Acme / Fraud" }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "Subscribers" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("shows the Subscribers folder tab with an empty state", async () => {
    renderApp("/folders/fraud-id/cards", {
      folders,
      permissions: ["card:read"],
      handlers: [indexHandler([], [])],
    });
    expect(
      await screen.findByRole("heading", { name: "No subscriber cards yet" }),
    ).toBeVisible();
  });

  it("creates a card straight in the Team of its Subscribers tab", async () => {
    const user = userEvent.setup();
    let createdIn: unknown = null;
    renderApp("/folders/fraud-id/cards", {
      folders,
      permissions: cardPermissions,
      handlers: [
        indexHandler([], []),
        http.post("*/api/v1/contracts/folders/:id/cards", ({ params }) => {
          createdIn = params.id;
          return HttpResponse.json(makeCard({ id: "card-2" }), {
            status: 201,
          });
        }),
        ...cardHandlers(makeCard({ id: "card-2" })),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create card" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    expect(within(dialog).queryByLabelText("Folder")).toBeNull();
    await user.type(within(dialog).getByLabelText("Name"), "ops");
    await user.type(within(dialog).getByLabelText("Description"), "Ops team");
    await user.click(
      within(dialog).getByRole("button", { name: "Create card" }),
    );
    await waitFor(() => {
      expect(createdIn).toBe("fraud-id");
    });
  });

  it("hides the audit trail without an audit scope", async () => {
    renderApp("/cards/card-1", {
      folders,
      permissions: cardPermissions,
      handlers: [...cardHandlers(makeCard())],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: makeCard().name }),
    ).toBeVisible();
    expect(screen.queryByRole("link", { name: /Audit trail/ })).toBeNull();
  });

  it("renders the card overview with an audit link and archives it", async () => {
    const user = userEvent.setup();
    let archived = false;
    renderApp("/cards/card-1", {
      folders,
      permissions: [...cardPermissions, "audit:query"],
      handlers: [
        http.post("*/api/v1/contracts/cards/:id/archive", () => {
          archived = true;
          return HttpResponse.json(
            makeCard({ archived_at: "2026-11-05T00:00:00Z" }),
          );
        }),
        ...cardHandlers(makeCard()),
      ],
    });
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "demo-analytics-team",
      }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "a@example.com" })).toHaveAttribute(
      "href",
      "mailto:a@example.com",
    );
    expect(
      await screen.findByRole("link", { name: /Audit trail/ }),
    ).toHaveAttribute("href", "/audit?aggregate_type=card&aggregate_id=card-1");
    await user.click(
      await screen.findByRole("button", { name: "Archive card" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Archive demo-analytics-team?",
    });
    expect(
      within(dialog).getByRole("button", { name: "Keep card" }),
    ).toHaveFocus();
    await user.click(
      within(dialog).getByRole("button", { name: "Archive card" }),
    );
    await waitFor(() => {
      expect(archived).toBe(true);
    });
  });

  it("subscribes to a published version, moves the pin and unsubscribes", async () => {
    const user = userEvent.setup();
    const calls: { path: string; body: unknown }[] = [];
    const record =
      (status = 200) =>
      async ({ request }: { request: Request }) => {
        calls.push({
          path: new URL(request.url).pathname,
          body: await request.json().catch(() => null),
        });
        return HttpResponse.json(subscription, { status });
      };
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({
            items: [
              {
                id: "contract-1",
                name: "Orders",
                odcs_id: "urn:orders",
                latest_version: { version_id: "v-2", semver: "1.1.0" },
              },
            ],
            next_cursor: null,
          }),
        ),
        http.post("*/api/v1/contracts/cards/:id/subscriptions", record(201)),
        http.post(
          "*/api/v1/contracts/cards/:id/subscriptions/:sid/move",
          record(),
        ),
        http.post(
          "*/api/v1/contracts/cards/:id/subscriptions/:sid/unsubscribe",
          record(),
        ),
        ...cardHandlers(makeCard(), [subscription]),
      ],
    });
    expect(await screen.findByRole("link", { name: "Orders" })).toHaveAttribute(
      "href",
      "/contracts/contract-1",
    );
    expect(
      await screen.findByText("Deprecated, sunset 2027-01-01"),
    ).toBeVisible();

    await user.click(
      screen.getByRole("button", { name: "Subscribe to a contract" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Subscribe to a contract",
    });
    await user.click(
      await within(dialog).findByRole("button", { name: /Orders/ }),
    );
    const select = await within(dialog).findByLabelText("Version");
    expect(select).toHaveValue("1.1.0");
    await user.selectOptions(select, "1.0.0");
    await user.click(
      within(dialog).getByRole("button", { name: "Subscribe card" }),
    );
    await waitFor(() => {
      expect(calls[0]).toEqual({
        path: "/api/v1/contracts/cards/card-1/subscriptions",
        body: { contract_id: "contract-1", mode: "pinned", version: "1.0.0" },
      });
    });

    await user.click(screen.getByRole("button", { name: "Move pin 1.0.0" }));
    const move = await screen.findByRole("dialog", { name: "Move pin" });
    await user.selectOptions(
      await within(move).findByLabelText("Version"),
      "1.1.0",
    );
    expect(
      within(move).getByText("Move the pin from 1.0.0 to 1.1.0?"),
    ).toBeVisible();
    await user.click(within(move).getByRole("button", { name: "Move pin" }));
    await waitFor(() => {
      expect(calls[1]).toEqual({
        path: "/api/v1/contracts/cards/card-1/subscriptions/sub-1/move",
        body: { version: "1.1.0" },
      });
    });

    await user.click(
      screen.getByRole("button", { name: "Unsubscribe from 1.0.0" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Unsubscribe?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Unsubscribe card" }),
    );
    await waitFor(() => {
      expect(calls[2]?.path).toBe(
        "/api/v1/contracts/cards/card-1/subscriptions/sub-1/unsubscribe",
      );
    });
  });

  it("adds and removes a member", async () => {
    const user = userEvent.setup();
    let added: unknown = null;
    let removed = "";
    renderApp("/cards/card-1?tab=members", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.post(
          "*/api/v1/contracts/cards/:id/members",
          async ({ request }) => {
            added = await request.json();
            return HttpResponse.json({}, { status: 201 });
          },
        ),
        http.delete(
          "*/api/v1/contracts/cards/:id/members/:type/:member",
          ({ params }) => {
            removed = `${String(params.type)}/${String(params.member)}`;
            return new HttpResponse(null, { status: 204 });
          },
        ),
        http.get("*/api/v1/identity/service-accounts", () =>
          HttpResponse.json({
            items: [
              {
                id: "sa-1",
                principal_id: "sa:sa-1",
                name: "loader-bot",
                is_active: true,
              },
            ],
            next_cursor: null,
          }),
        ),
        ...cardHandlers(
          makeCard({
            members: [
              {
                member_type: "local_group",
                member_id: "grp-1",
                added_by: "local:admin",
                added_at: "2026-11-01T10:00:00Z",
              },
            ],
          }),
        ),
      ],
    });
    await screen.findByText("grp-1");
    await user.click(await screen.findByRole("button", { name: "Add member" }));
    const dialog = await screen.findByRole("dialog", { name: "Add member" });
    await user.selectOptions(
      within(dialog).getByLabelText("Principal type"),
      "service_account",
    );
    await user.type(
      within(dialog).getByLabelText("Search service accounts"),
      "loader",
    );
    await user.click(
      await within(dialog).findByRole("option", { name: /loader-bot/ }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Add member" }),
    );
    await waitFor(() => {
      expect(added).toEqual({
        member_type: "service_account",
        member_id: "sa:sa-1",
      });
    });
    await user.click(
      screen.getByRole("button", { name: "Remove member grp-1" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Remove member?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Remove member" }),
    );
    await waitFor(() => {
      expect(removed).toBe("local_group/grp-1");
    });
  });

  it("shows notices with delivery state", async () => {
    renderApp("/cards/card-1?tab=notices", {
      folders,
      permissions: cardPermissions,
      handlers: cardHandlers(makeCard()),
    });
    expect(await screen.findByText("version.deprecated")).toBeVisible();
    expect(screen.getByText("Email: delivered")).toBeVisible();
    expect(screen.getByText("Version 1.0.0")).toBeVisible();
  });

  it("rotates the webhook secret and shows it once", async () => {
    const user = userEvent.setup();
    renderApp("/cards/card-1?tab=webhook", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.post("*/api/v1/contracts/cards/:id/webhook-secret", () =>
          HttpResponse.json({
            webhook_secret: "whsec_new_secret",
            generation: 2,
            previous_valid_until: "2026-11-16T10:00:00Z",
          }),
        ),
        ...cardHandlers(
          makeCard({
            webhook_enabled: true,
            webhook_url: "https://hooks.example.com/x",
            webhook_secret_configured: true,
          }),
        ),
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
    const reveal = await screen.findByRole("dialog", {
      name: /webhook secret/i,
    });
    expect(within(reveal).getByDisplayValue("whsec_new_secret")).toBeVisible();
    expect(screen.getByText("Previous secret still accepted")).toBeVisible();
  });

  it("renders Not found for a hidden card", async () => {
    renderApp("/cards/missing", {
      folders,
      handlers: [
        http.get("*/api/v1/contracts/cards/:id", () =>
          HttpResponse.json(
            {
              type: "urn:dc:problem:not-found",
              title: "Not Found",
              status: 404,
            },
            {
              status: 404,
              headers: { "Content-Type": "application/problem+json" },
            },
          ),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Not found" }),
    ).toBeVisible();
  });
});
