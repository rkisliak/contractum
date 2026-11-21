// The card page tabs through the real router: Members (gating, add / remove with validation and
// failures), Notices (loading, error, empty, paging, message and delivery variants) and Webhook
// (settings, gating, rotate failure, secret shown once and cleared).
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { CardNotice, SubscriberCard } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, makeMe, renderApp, rootFolder } from "@/test/renderApp";

afterEach(() => {
  tokenStore.clear();
});

const fraud = makeFolder({
  id: "fraud-id",
  kind: "team",
  display_name: "Fraud",
  path: "fraud",
});
const folders = [rootFolder, fraud];
const manage = ["card:read", "card:manage"];
const readOnly = ["card:read"];

function makeCard(overrides: Partial<SubscriberCard> = {}): SubscriberCard {
  return {
    id: "card-1",
    folder_id: "fraud-id",
    name: "demo-analytics-team",
    description: "Analytics team",
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

const archivedAt = "2026-11-05T10:00:00Z";
const member = {
  member_type: "local_group" as const,
  member_id: "grp-1",
  added_by: "local:admin",
  added_at: "2026-11-01T10:00:00Z",
};

function group(id: string, name: string) {
  return {
    id,
    name,
    description: "",
    owner_folder_id: null,
    created_at: "2026-11-01T10:00:00Z",
  };
}

// One local group and one service account for the picker and the member names.
const pickerLists = [
  http.get("*/api/v1/identity/groups", () =>
    HttpResponse.json({
      items: [group("grp-1", "Data engineers")],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/identity/service-accounts", () =>
    HttpResponse.json({
      items: [
        {
          id: "sa-9",
          principal_id: "sa:sa-9",
          name: "pipeline-bot",
          is_active: true,
        },
      ],
      next_cursor: null,
    }),
  ),
];

function problem(status: number, title: string, extra: object = {}) {
  return HttpResponse.json(
    { type: "urn:dc:problem:conflict", title, status, ...extra },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

function cardHandler(card: SubscriberCard) {
  return http.get("*/api/v1/contracts/cards/:id", () =>
    HttpResponse.json(card),
  );
}

function notice(id: string, overrides: Partial<CardNotice> = {}): CardNotice {
  return {
    id,
    contract_id: "contract-1",
    version_id: "v-1",
    event_type: "version.deprecated",
    occurred_at: "2026-11-03T10:00:00Z",
    created_at: "2026-11-03T10:00:00Z",
    payload: {},
    deliveries: [],
    ...overrides,
  };
}

type Delivery = CardNotice["deliveries"][number];

// `status` is a plain string so a value the UI does not know yet can be exercised.
function delivery(channel: Delivery["channel"], status: string): Delivery {
  return {
    channel,
    status,
    attempts: 1,
    delivered_at: null,
    last_attempt_at: null,
    last_error: null,
    last_status: null,
  } as unknown as Delivery;
}

describe("Members tab", () => {
  it("offers Add member from the empty state to a manager, validates and shows a refusal", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/cards/card-1?tab=members", {
      folders,
      permissions: manage,
      handlers: [
        cardHandler(makeCard()),
        ...pickerLists,
        http.post(
          "*/api/v1/contracts/cards/:id/members",
          async ({ request }) => {
            bodies.push(await request.json());
            return problem(422, "Principal cannot be linked", {
              detail: "The group does not exist.",
            });
          },
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "No members yet" }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Link a group or service account so its members can read this card and manage its subscriptions.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Add member" }));
    const dialog = await screen.findByRole("dialog", { name: "Add member" });
    // Members read the card and manage its subscriptions; they never manage the card itself.
    expect(
      within(dialog).getByText(
        "Members can read this card and manage its subscriptions. Only the creator and folder admins can edit it, archive it or change its members and webhook.",
      ),
    ).toBeVisible();
    expect(within(dialog).queryByText(/can manage this card/)).toBeNull();
    // The picker offers exactly the card member types, local groups first.
    const type = within(dialog).getByLabelText("Principal type");
    expect(
      within(type)
        .getAllByRole("option")
        .map((option) => option.getAttribute("value")),
    ).toEqual(["local_group", "entra_group", "service_account"]);
    // Search-only: nothing is listed before two characters.
    expect(within(dialog).queryByRole("listbox")).toBeNull();
    await user.type(
      within(dialog).getByLabelText("Search local groups"),
      "data",
    );
    expect(
      await within(dialog).findByRole("option", { name: /Data engineers/ }),
    ).toBeVisible();

    // Nothing picked: a validation message, nothing sent.
    await user.click(
      within(dialog).getByRole("button", { name: "Add member" }),
    );
    expect(
      await within(dialog).findByText("Choose a group or service account."),
    ).toBeVisible();
    expect(bodies).toEqual([]);

    await user.selectOptions(type, "service_account");
    await user.type(
      within(dialog).getByLabelText("Search service accounts"),
      "pipe",
    );
    await user.click(
      await within(dialog).findByRole("option", { name: /pipeline-bot/ }),
    );
    expect(
      within(dialog).queryByText("Choose a group or service account."),
    ).toBeNull();
    await user.click(
      within(dialog).getByRole("button", { name: "Add member" }),
    );
    expect(
      await within(dialog).findByText("Principal cannot be linked"),
    ).toBeVisible();
    expect(within(dialog).getByText("The group does not exist.")).toBeVisible();
    expect(bodies).toEqual([
      { member_type: "service_account", member_id: "sa:sa-9" },
    ]);

    // Go back resets the form; reopening starts clean.
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Add member" })).toBeNull();
    });
    await user.click(screen.getByRole("button", { name: "Add member" }));
    const again = await screen.findByRole("dialog", { name: "Add member" });
    expect(within(again).getByLabelText("Principal type")).toHaveValue(
      "local_group",
    );
    expect(within(again).queryByText("Principal cannot be linked")).toBeNull();
    await user.type(
      within(again).getByLabelText("Search local groups"),
      "data",
    );
    expect(
      await within(again).findByRole("option", { name: /Data engineers/ }),
    ).toHaveAttribute("aria-selected", "false");
    // The first Escape closes the open results list only; the second closes the dialog.
    await user.keyboard("{Escape}");
    expect(within(again).queryByRole("listbox")).toBeNull();
    expect(screen.getByRole("dialog", { name: "Add member" })).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Add member" })).toBeNull();
    });
  });

  it("adds a member picked past the first page of groups", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    const cursors: (string | null)[] = [];
    renderApp("/cards/card-1?tab=members", {
      folders,
      permissions: manage,
      handlers: [
        cardHandler(makeCard()),
        http.get("*/api/v1/identity/groups", ({ request }) => {
          const url = new URL(request.url);
          const cursor = url.searchParams.get("cursor");
          cursors.push(cursor);
          expect(url.searchParams.get("limit")).toBe("100");
          return HttpResponse.json(
            cursor === null
              ? {
                  items: Array.from({ length: 100 }, (_, i) =>
                    group(`grp-${String(i)}`, `Group ${String(i)}`),
                  ),
                  next_cursor: "page-2",
                }
              : { items: [group("grp-7x", "Late team")], next_cursor: null },
          );
        }),
        http.post(
          "*/api/v1/contracts/cards/:id/members",
          async ({ request }) => {
            body = await request.json();
            return HttpResponse.json({}, { status: 201 });
          },
        ),
      ],
    });
    await user.click(await screen.findByRole("button", { name: "Add member" }));
    const dialog = await screen.findByRole("dialog", { name: "Add member" });
    // 101 matches for "gr": only the first 20 are rendered until the search narrows them.
    await user.type(within(dialog).getByLabelText("Search local groups"), "gr");
    expect(
      await within(dialog).findByText(
        "Showing 20 of 101 matches. Type more to narrow the list.",
      ),
    ).toBeVisible();
    await user.clear(within(dialog).getByLabelText("Search local groups"));
    await user.type(
      within(dialog).getByLabelText("Search local groups"),
      "late",
    );
    await user.click(
      await within(dialog).findByRole("option", { name: /Late team/ }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Add member" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Add member" })).toBeNull();
    });
    expect(body).toEqual({ member_type: "local_group", member_id: "grp-7x" });
    // The picker and the member-name map each follow the cursor to the second page.
    expect(cursors).toContain("page-2");
    expect(cursors.filter((c) => c !== null && c !== "page-2")).toEqual([]);
  });

  it("names members from the identity lists and falls back to the id", async () => {
    renderApp("/cards/card-1?tab=members", {
      folders,
      permissions: manage,
      handlers: [
        cardHandler(
          makeCard({
            members: [
              { ...member, member_id: "grp-1" },
              {
                ...member,
                member_type: "service_account",
                member_id: "sa:sa-9",
              },
              { ...member, member_id: "grp-gone" },
            ],
          }),
        ),
        ...pickerLists,
      ],
    });
    const table = await screen.findByRole("table", {
      name: "Members of demo-analytics-team",
    });
    expect(await within(table).findByText("Data engineers")).toBeVisible();
    expect(within(table).getByText("pipeline-bot")).toBeVisible();
    expect(within(table).getByText("grp-gone")).toBeVisible();
    expect(
      within(table).getByRole("button", {
        name: "Remove member Data engineers",
      }),
    ).toBeVisible();
  });

  it("lists members with type labels and keeps the remove dialog open when removal fails", async () => {
    const user = userEvent.setup();
    let removed = "";
    renderApp("/cards/card-1?tab=members", {
      folders,
      permissions: manage,
      handlers: [
        cardHandler(
          makeCard({
            members: [
              member,
              {
                ...member,
                member_type: "mystery" as never,
                member_id: "odd-1",
              },
            ],
          }),
        ),
        http.delete(
          "*/api/v1/contracts/cards/:id/members/:type/:member",
          ({ params }) => {
            removed = `${String(params.type)}/${String(params.member)}`;
            return problem(409, "Last member", {
              detail: "A card keeps at least one member.",
            });
          },
        ),
      ],
    });
    const table = await screen.findByRole("table", {
      name: "Members of demo-analytics-team",
    });
    expect(within(table).getByText("Local group")).toBeVisible();
    // An unknown type falls back to the raw value.
    expect(within(table).getByText("mystery")).toBeVisible();
    expect(within(table).getAllByText("local:admin")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Add member" })).toBeVisible();

    await user.click(
      within(table).getByRole("button", { name: "Remove member grp-1" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Remove member?",
    });
    expect(
      within(confirm).getByText(
        "grp-1 loses its member access to demo-analytics-team.",
      ),
    ).toBeVisible();
    await user.click(
      within(confirm).getByRole("button", { name: "Remove member" }),
    );
    expect(await within(confirm).findByText("Last member")).toBeVisible();
    expect(removed).toBe("local_group/grp-1");

    await user.click(
      within(confirm).getByRole("button", { name: "Keep member" }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("alertdialog", { name: "Remove member?" }),
      ).toBeNull();
    });
  });

  it("lets the card's creator manage members without card:manage", async () => {
    const me = makeMe();
    renderApp("/cards/card-1?tab=members", {
      folders,
      me,
      permissions: readOnly,
      handlers: [
        cardHandler(
          makeCard({ created_by: me.principal_id, members: [member] }),
        ),
      ],
    });
    expect(
      await screen.findByRole("button", { name: "Remove member grp-1" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Add member" })).toBeVisible();
  });

  it("hides every member action without card:manage", async () => {
    renderApp("/cards/card-1?tab=members", {
      folders,
      permissions: readOnly,
      handlers: [cardHandler(makeCard({ members: [member] }))],
    });
    expect(await screen.findByText("grp-1")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Add member" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Remove member grp-1" }),
    ).toBeNull();
  });

  it("hides member actions on an archived card, even for a manager", async () => {
    renderApp("/cards/card-1?tab=members", {
      folders,
      permissions: manage,
      handlers: [cardHandler(makeCard({ archived_at: archivedAt }))],
    });
    expect(
      await screen.findByRole("heading", { name: "No members yet" }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Add member" })).toBeNull();
  });
});

describe("Overview tab", () => {
  it("reads delivery, the archive record and every contact kind, and moves tabs through the URL", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/cards/card-1", {
      folders,
      permissions: readOnly,
      handlers: [
        http.get("*/api/v1/contracts/cards/:id/notices", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
        cardHandler(
          makeCard({
            email_enabled: true,
            webhook_enabled: true,
            archived_at: archivedAt,
            archived_by: "local:admin",
            contacts: [
              { kind: "url", name: null, value: "https://wiki.example.com/t" },
              { kind: "url", name: "Runbook", value: "wiki/team" },
              { kind: "text", name: null, value: "Slack #data" },
            ],
          }),
        ),
      ],
    });
    expect(await screen.findByText("Email, Webhook")).toBeVisible();
    expect(screen.getByText("This card is archived")).toBeVisible();
    expect(
      screen.getByRole("link", { name: "https://wiki.example.com/t" }),
    ).toHaveAttribute("href", "https://wiki.example.com/t");
    // A link contact that is not an http(s) URL stays plain text.
    expect(screen.getByText("wiki/team")).not.toHaveAttribute("href");
    expect(screen.getByText("Link")).toBeVisible();
    expect(screen.getByText("Runbook")).toBeVisible();
    expect(screen.getByText("Text")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Notices" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ tab: "notices" });
    });
    expect(
      await screen.findByRole("heading", { name: "No notices yet" }),
    ).toBeVisible();
  });

  it("says the archiver is unknown when the record has none", async () => {
    renderApp("/cards/card-1", {
      folders,
      permissions: readOnly,
      handlers: [
        cardHandler(makeCard({ archived_at: archivedAt, archived_by: null })),
      ],
    });
    expect(await screen.findByText(/ by unknown$/)).toBeVisible();
    expect(screen.getByText("In-app notices only")).toBeVisible();
  });

  it("retries a failed card load", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/cards/card-1", {
      folders,
      permissions: readOnly,
      handlers: [
        http.get("*/api/v1/contracts/cards/:id", () => {
          calls += 1;
          return calls === 1
            ? problem(500, "Card unavailable")
            : HttpResponse.json(makeCard());
        }),
      ],
    });
    expect(await screen.findByText("Card unavailable")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "demo-analytics-team",
      }),
    ).toBeVisible();
  });
});

describe("Notices tab", () => {
  it("shows a loading skeleton, then the empty state", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    renderApp("/cards/card-1?tab=notices", {
      folders,
      permissions: readOnly,
      handlers: [
        cardHandler(makeCard()),
        http.get("*/api/v1/contracts/cards/:id/notices", async () => {
          await gate;
          return HttpResponse.json({ items: [], next_cursor: null });
        }),
      ],
    });
    expect(await screen.findByText("Loading notices")).toBeInTheDocument();
    release();
    expect(
      await screen.findByRole("heading", { name: "No notices yet" }),
    ).toBeVisible();
  });

  it("shows a load failure with Retry loading, which fetches again", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/cards/card-1?tab=notices", {
      folders,
      permissions: readOnly,
      handlers: [
        cardHandler(makeCard()),
        http.get("*/api/v1/contracts/cards/:id/notices", () => {
          calls += 1;
          return calls === 1
            ? problem(500, "Notices unavailable")
            : HttpResponse.json({
                items: [notice("n-1", { payload: { message: "Recovered" } })],
                next_cursor: null,
              });
        }),
      ],
    });
    expect(await screen.findByText("Notices unavailable")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(await screen.findByText("Recovered")).toBeVisible();
    expect(calls).toBe(2);
  });

  it("renders messages, deliveries and contract links, and pages with the cursor", async () => {
    const user = userEvent.setup();
    const cursors: (string | null)[] = [];
    const limits: (string | null)[] = [];
    renderApp("/cards/card-1?tab=notices", {
      folders,
      permissions: readOnly,
      handlers: [
        cardHandler(makeCard()),
        http.get("*/api/v1/contracts/cards/:id/notices", ({ request }) => {
          const query = new URL(request.url).searchParams;
          cursors.push(query.get("cursor"));
          limits.push(query.get("limit"));
          if (query.get("cursor") === "page-2") {
            return HttpResponse.json({
              items: [
                notice("n-5", {
                  event_type: "pin.removed",
                  payload: { note: "Removed by owner" },
                }),
              ],
              next_cursor: null,
            });
          }
          return HttpResponse.json({
            items: [
              notice("n-1", {
                payload: { message: "", summary: "Superseded by 2.0.0" },
                deliveries: [
                  delivery("email", "delivered"),
                  delivery("webhook", "failed"),
                ],
              }),
              notice("n-2", {
                contract_id: "contract-2",
                payload: { reason: "Unpublished" },
                deliveries: [delivery("webhook", "weird")],
              }),
              notice("n-3", { payload: { semver: "1.0.0" } }),
              notice("n-4", { payload: { semver: 3 } }),
            ],
            next_cursor: "page-2",
          });
        }),
      ],
    });
    const table = await screen.findByRole("table", {
      name: "Notices for demo-analytics-team",
    });
    expect(within(table).getByText("Superseded by 2.0.0")).toBeVisible();
    expect(within(table).getByText("Unpublished")).toBeVisible();
    expect(within(table).getByText("Version 1.0.0")).toBeVisible();
    expect(within(table).getByText("Email: delivered")).toBeVisible();
    expect(within(table).getByText("Webhook: failed")).toBeVisible();
    expect(within(table).getByText("Webhook: weird")).toBeVisible();
    expect(within(table).getAllByText("In-app only")).toHaveLength(2);
    const links = within(table).getAllByRole("link", { name: "Open contract" });
    expect(links[0]).toHaveAttribute("href", "/contracts/contract-1");
    expect(links[1]).toHaveAttribute("href", "/contracts/contract-2");
    expect(within(table).getAllByRole("row")).toHaveLength(5);
    expect(cursors).toEqual([null]);
    expect(limits).toEqual(["50"]);

    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(await within(table).findByText("Removed by owner")).toBeVisible();
    expect(cursors).toEqual([null, "page-2"]);
    expect(screen.getByText("All results loaded")).toBeVisible();
  });

  it("keeps loaded notices and shows a later page failure above the footer", async () => {
    const user = userEvent.setup();
    renderApp("/cards/card-1?tab=notices", {
      folders,
      permissions: readOnly,
      handlers: [
        cardHandler(makeCard()),
        http.get("*/api/v1/contracts/cards/:id/notices", ({ request }) =>
          new URL(request.url).searchParams.get("cursor") === null
            ? HttpResponse.json({
                items: [notice("n-1", { payload: { message: "First" } })],
                next_cursor: "page-2",
              })
            : problem(500, "Page two failed"),
        ),
      ],
    });
    expect(await screen.findByText("First")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(await screen.findByText("Page two failed")).toBeVisible();
    expect(screen.getByText("First")).toBeVisible();
  });
});

describe("Webhook tab", () => {
  it("shows unset delivery settings and opens the edit dialog from Edit delivery settings", async () => {
    const user = userEvent.setup();
    renderApp("/cards/card-1?tab=webhook", {
      folders,
      permissions: manage,
      handlers: [cardHandler(makeCard())],
    });
    expect(await screen.findByText("Off")).toBeVisible();
    expect(screen.getByText("Not set")).toBeVisible();
    expect(screen.getByText("Not created yet")).toBeVisible();
    // The signed-in test user is interactive, so a manager may rotate.
    expect(screen.getByRole("button", { name: "Rotate secret" })).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Edit delivery settings" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Edit card" });
    expect(
      within(dialog).getByLabelText("Deliver notices to a webhook"),
    ).not.toBeChecked();
  });

  it("hides Rotate secret for a non-interactive manager but keeps Edit delivery settings", async () => {
    renderApp("/cards/card-1?tab=webhook", {
      folders,
      me: makeMe({ interactive: false }),
      permissions: manage,
      handlers: [
        cardHandler(
          makeCard({
            webhook_enabled: true,
            webhook_url: "https://hooks.example.com/x",
            webhook_secret_configured: true,
          }),
        ),
      ],
    });
    expect(await screen.findByText("Enabled")).toBeVisible();
    expect(screen.getByText("https://hooks.example.com/x")).toBeVisible();
    expect(screen.getByText("Configured")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Edit delivery settings" }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Rotate secret" })).toBeNull();
  });

  it("offers no webhook action to a reader", async () => {
    renderApp("/cards/card-1?tab=webhook", {
      folders,
      permissions: readOnly,
      handlers: [cardHandler(makeCard())],
    });
    expect(await screen.findByText("Off")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Edit delivery settings" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Rotate secret" })).toBeNull();
  });

  it("offers no webhook action on an archived card", async () => {
    renderApp("/cards/card-1?tab=webhook", {
      folders,
      permissions: manage,
      handlers: [cardHandler(makeCard({ archived_at: archivedAt }))],
    });
    expect(await screen.findByText("Off")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Edit delivery settings" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Rotate secret" })).toBeNull();
  });

  it("keeps the confirmation open with the problem when rotation fails", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/cards/card-1?tab=webhook", {
      folders,
      permissions: manage,
      handlers: [
        cardHandler(makeCard({ webhook_secret_configured: true })),
        http.post("*/api/v1/contracts/cards/:id/webhook-secret", () => {
          calls += 1;
          return problem(409, "Rotation refused", {
            detail: "Webhook delivery is off.",
          });
        }),
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
    expect(await within(confirm).findByText("Rotation refused")).toBeVisible();
    expect(calls).toBe(1);
    expect(screen.queryByText("Previous secret still accepted")).toBeNull();
    expect(
      screen.queryByRole("dialog", { name: /webhook secret/i }),
    ).toBeNull();
  });

  it("shows the rotated secret once and forgets it after Close after storing", async () => {
    const user = userEvent.setup();
    renderApp("/cards/card-1?tab=webhook", {
      folders,
      permissions: manage,
      handlers: [
        cardHandler(makeCard({ webhook_secret_configured: true })),
        http.post("*/api/v1/contracts/cards/:id/webhook-secret", () =>
          HttpResponse.json({
            webhook_secret: "whsec_rotated",
            generation: 2,
            previous_valid_until: "2026-11-16T10:00:00Z",
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
    expect(within(reveal).getByDisplayValue("whsec_rotated")).toBeVisible();
    await user.click(
      within(reveal).getByLabelText("I have stored this webhook secret"),
    );
    await user.click(
      within(reveal).getByRole("button", {
        name: "Close after storing webhook secret",
      }),
    );
    await waitFor(() => {
      expect(screen.queryByDisplayValue("whsec_rotated")).toBeNull();
    });
    expect(screen.getByText("Previous secret still accepted")).toBeVisible();
  });
});
