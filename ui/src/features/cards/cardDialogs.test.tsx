// The card dialogs through the real router: SubscribeDialog (search, version picker, every
// validation and API failure path), MoveDialog and CardFormDialog in edit and create mode.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { CardSubscription, Me, SubscriberCard } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import {
  makeFolder,
  makeMe,
  renderApp,
  rootFolder,
  ROOT_ID,
} from "@/test/renderApp";

afterEach(() => {
  tokenStore.clear();
});

// The subscriptions table resolves "Pinned by" names from the identity lists; none here.
const principalLists = http.get(
  /\/api\/v1\/identity\/(users|groups|service-accounts|entra\/users|entra\/groups|entra\/service-principals)$/,
  () => HttpResponse.json({ items: [], next_cursor: null }),
);

const acme = makeFolder({ id: "acme-id", display_name: "Acme", path: "acme" });
const fraud = makeFolder({
  id: "fraud-id",
  parent_id: "acme-id",
  kind: "team",
  display_name: "Fraud",
  path: "acme/fraud",
});
const risk = makeFolder({
  id: "risk-id",
  parent_id: "acme-id",
  kind: "team",
  display_name: "Risk",
  path: "acme/risk",
});
const folders = [rootFolder, acme, fraud, risk];
const cardPermissions = ["card:read", "card:create", "card:manage"];

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

function problem(status: number, title: string, extra: object = {}) {
  return HttpResponse.json(
    { type: "urn:dc:problem:conflict", title, status, ...extra },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

function deferred() {
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { gate, release };
}

const orders = {
  id: "contract-1",
  name: "Orders",
  odcs_id: "urn:orders",
  latest_version: { version_id: "v-2", semver: "1.1.0" },
};
const unnamed = {
  id: "contract-2",
  name: null,
  odcs_id: "urn:unnamed",
  latest_version: null,
};

type Versions = ReturnType<typeof version>[];

function cardHandlers(
  card: SubscriberCard,
  {
    subs = [subscription],
    versions = [
      version("2.0.0", "v-9", { lifecycle: "draft" }),
      version("1.2.0", "v-3", { status: "preview" }),
      version("1.1.0", "v-2", { status: "latest" }),
      version("1.0.0", "v-1", { deprecated: true }),
    ],
  }: { subs?: CardSubscription[]; versions?: Versions } = {},
) {
  return [
    principalLists,
    http.get("*/api/v1/contracts/cards/:id", () => HttpResponse.json(card)),
    http.get("*/api/v1/contracts/cards/:id/subscriptions", () =>
      HttpResponse.json({ items: subs, next_cursor: null }),
    ),
    http.get("*/api/v1/contracts/contracts/:id/versions", () =>
      HttpResponse.json({ items: versions }),
    ),
    http.get("*/api/v1/contracts/contracts/:id", () =>
      HttpResponse.json(orders),
    ),
  ];
}

async function openSubscribe() {
  const user = userEvent.setup();
  // An empty list also offers the action in its empty state; the toolbar one comes first.
  const [button] = await screen.findAllByRole("button", {
    name: "Subscribe to a contract",
  });
  await user.click(button as HTMLElement);
  const dialog = await screen.findByRole("dialog", {
    name: "Subscribe to a contract",
  });
  return { user, dialog };
}

describe("SubscribeDialog", () => {
  it("searches contracts, lists only published versions with their status and validates the choice", async () => {
    const searches: URLSearchParams[] = [];
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.get("*/api/v1/contracts/contracts", ({ request }) => {
          const query = new URL(request.url).searchParams;
          searches.push(query);
          return HttpResponse.json({
            items: query.get("q") === "zzz" ? [] : [orders, unnamed],
            next_cursor: null,
          });
        }),
        ...cardHandlers(makeCard()),
      ],
    });
    const { user, dialog } = await openSubscribe();
    expect(
      within(dialog).getByText(
        "Pin a published contract version to demo-analytics-team. The card is notified when that version changes.",
      ),
    ).toBeVisible();
    const list = await within(dialog).findByRole("list", {
      name: "Matching contracts",
    });
    expect(searches[0]?.get("limit")).toBe("20");
    expect(searches[0]?.has("q")).toBe(false);
    expect(within(list).getByText("urn:orders · latest 1.1.0")).toBeVisible();
    // A contract without a name shows its ODCS id; one never published says so.
    expect(
      within(list).getByRole("button", { name: /urn:unnamed · not published/ }),
    ).toBeVisible();

    // Nothing chosen yet.
    await user.click(
      within(dialog).getByRole("button", { name: "Subscribe card" }),
    );
    expect(
      await within(dialog).findByText("Choose a contract first."),
    ).toBeVisible();

    // An unpublished contract leaves the version empty.
    await user.click(within(list).getByRole("button", { name: /urn:unnamed/ }));
    expect(within(dialog).queryByText("Choose a contract first.")).toBeNull();
    expect(
      within(list).getByRole("button", { name: /urn:unnamed/ }),
    ).toHaveAttribute("aria-pressed", "true");
    const select = await within(dialog).findByLabelText("Version");
    expect(select).toHaveValue("");
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([
      "Choose a version",
      "1.2.0 (preview)",
      "1.1.0 (latest)",
      "1.0.0 · deprecated",
    ]);
    expect(
      within(dialog).getByText("A pin is always one published version."),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Subscribe card" }),
    );
    expect(
      await within(dialog).findByText("Choose a published version to pin."),
    ).toBeVisible();
    await user.selectOptions(select, "1.2.0");
    expect(
      within(dialog).queryByText("Choose a published version to pin."),
    ).toBeNull();

    // Typing narrows the search with q; no match says so.
    await user.type(within(dialog).getByLabelText("Find a contract"), "zzz");
    expect(
      await within(dialog).findByText("No contracts match."),
    ).toBeVisible();
    expect(searches.at(-1)?.get("q")).toBe("zzz");
  });

  it("shows a searching spinner, then a search failure as a problem", async () => {
    const { gate, release } = deferred();
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.get("*/api/v1/contracts/contracts", async () => {
          await gate;
          return problem(500, "Search broke");
        }),
        ...cardHandlers(makeCard()),
      ],
    });
    const { dialog } = await openSubscribe();
    expect(
      await within(dialog).findByText("Searching contracts"),
    ).toBeInTheDocument();
    release();
    expect(await within(dialog).findByText("Search broke")).toBeVisible();
  });

  it("shows a version loading spinner, a versions failure and a contract with no published version", async () => {
    const { gate, release } = deferred();
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({ items: [orders, unnamed], next_cursor: null }),
        ),
        http.get(
          "*/api/v1/contracts/contracts/:id/versions",
          async ({ params }) => {
            if (params.id === "contract-2") {
              return HttpResponse.json({
                items: [version("0.1.0", "v-0", { lifecycle: "draft" })],
              });
            }
            await gate;
            return problem(500, "Versions unavailable");
          },
        ),
        ...cardHandlers(makeCard(), { subs: [] }),
      ],
    });
    const { user, dialog } = await openSubscribe();
    await user.click(
      await within(dialog).findByRole("button", { name: /Orders/ }),
    );
    expect(
      await within(dialog).findByText("Loading versions"),
    ).toBeInTheDocument();
    release();
    expect(
      await within(dialog).findByText("Versions unavailable"),
    ).toBeVisible();
    expect(within(dialog).queryByLabelText("Version")).toBeNull();

    // A contract with drafts only: the picker is disabled and says why.
    await user.click(
      within(dialog).getByRole("button", { name: /urn:unnamed/ }),
    );
    const select = await within(dialog).findByLabelText("Version");
    expect(select).toBeDisabled();
    expect(
      within(dialog).getByText(
        "This contract has no other published version to pin.",
      ),
    ).toBeVisible();
  });

  it("keeps the dialog open with the API problem when subscribing fails, and resets on Go back", async () => {
    let body: unknown = null;
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({ items: [orders], next_cursor: null }),
        ),
        http.post(
          "*/api/v1/contracts/cards/:id/subscriptions",
          async ({ request }) => {
            body = await request.json();
            return problem(409, "Already subscribed", {
              detail: "The card already pins this contract.",
            });
          },
        ),
        ...cardHandlers(makeCard(), { subs: [] }),
      ],
    });
    const { user, dialog } = await openSubscribe();
    await user.click(
      await within(dialog).findByRole("button", { name: /Orders/ }),
    );
    expect(await within(dialog).findByLabelText("Version")).toHaveValue(
      "1.1.0",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Subscribe card" }),
    );
    expect(await within(dialog).findByText("Already subscribed")).toBeVisible();
    expect(
      within(dialog).getByText("The card already pins this contract."),
    ).toBeVisible();
    expect(body).toEqual({
      contract_id: "contract-1",
      mode: "pinned",
      version: "1.1.0",
    });

    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Subscribe to a contract" }),
      ).toBeNull();
    });
    // Reopened from the empty state: nothing selected, no problem left over.
    const again = await openSubscribe();
    expect(within(again.dialog).queryByText("Already subscribed")).toBeNull();
    expect(within(again.dialog).queryByLabelText("Version")).toBeNull();
    expect(
      await within(again.dialog).findByRole("button", { name: /Orders/ }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("closes and resets on Escape", async () => {
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({ items: [orders], next_cursor: null }),
        ),
        ...cardHandlers(makeCard()),
      ],
    });
    const { user, dialog } = await openSubscribe();
    await user.type(within(dialog).getByLabelText("Find a contract"), "ord");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Subscribe to a contract" }),
      ).toBeNull();
    });
    const again = await openSubscribe();
    expect(within(again.dialog).getByLabelText("Find a contract")).toHaveValue(
      "",
    );
  });
});

describe("MoveDialog", () => {
  async function openMove() {
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Move pin 1.0.0" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Move pin" });
    return { user, dialog };
  }

  it("requires a version, excludes the current pin and shows a move failure", async () => {
    let body: unknown = null;
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.post(
          "*/api/v1/contracts/cards/:id/subscriptions/:sid/move",
          async ({ request }) => {
            body = await request.json();
            return problem(409, "Version not movable");
          },
        ),
        ...cardHandlers(makeCard()),
      ],
    });
    const { user, dialog } = await openMove();
    expect(
      within(dialog).getByText(
        "The card is pinned to 1.0.0. Choose the version to move to.",
      ),
    ).toBeVisible();
    const select = await within(dialog).findByLabelText("Version");
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Choose a version", "1.2.0 (preview)", "1.1.0 (latest)"]);
    await user.click(within(dialog).getByRole("button", { name: "Move pin" }));
    expect(
      await within(dialog).findByText("Choose the version to move to."),
    ).toBeVisible();
    await user.selectOptions(select, "1.2.0");
    expect(
      within(dialog).queryByText("Choose the version to move to."),
    ).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Move pin" }));
    expect(
      await within(dialog).findByText("Version not movable"),
    ).toBeVisible();
    expect(body).toEqual({ version: "1.2.0" });

    await user.click(
      within(dialog).getByRole("button", { name: "Keep 1.0.0" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Move pin" })).toBeNull();
    });
    const again = await openMove();
    expect(within(again.dialog).queryByText("Version not movable")).toBeNull();
    expect(await within(again.dialog).findByLabelText("Version")).toHaveValue(
      "",
    );
  });

  it("disables the picker when no other published version exists and closes on Escape", async () => {
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        ...cardHandlers(makeCard(), {
          versions: [version("1.0.0", "v-1")],
        }),
      ],
    });
    const { user, dialog } = await openMove();
    expect(await within(dialog).findByLabelText("Version")).toBeDisabled();
    expect(
      within(dialog).getByText(
        "This contract has no other published version to pin.",
      ),
    ).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Move pin" })).toBeNull();
    });
  });

  it("renders a versions failure inside the move dialog", async () => {
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.get("*/api/v1/contracts/contracts/:id/versions", () =>
          problem(500, "Versions down"),
        ),
        ...cardHandlers(makeCard()),
      ],
    });
    const { dialog } = await openMove();
    expect(await within(dialog).findByText("Versions down")).toBeVisible();
    expect(within(dialog).queryByLabelText("Version")).toBeNull();
  });
});

describe("empty response bodies", () => {
  const empty = () => new HttpResponse(null, { status: 204 });

  it("renders a generic problem when the contract search answers without a body", async () => {
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.get("*/api/v1/contracts/contracts", empty),
        ...cardHandlers(makeCard(), { subs: [] }),
      ],
    });
    const { dialog } = await openSubscribe();
    expect(
      await within(dialog).findByText("Something went wrong"),
    ).toBeVisible();
  });

  it("renders a generic problem when the versions answer without a body", async () => {
    const user = userEvent.setup();
    renderApp("/cards/card-1?tab=subscriptions", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.get("*/api/v1/contracts/contracts/:id/versions", empty),
        ...cardHandlers(makeCard()),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Move pin 1.0.0" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Move pin" });
    expect(
      await within(dialog).findByText("Something went wrong"),
    ).toBeVisible();
    expect(within(dialog).queryByLabelText("Version")).toBeNull();
  });
});

describe("CardFormDialog (edit)", () => {
  async function openEdit() {
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit card" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit card" });
    return { user, dialog };
  }

  it("validates every field, edits contacts and delivery and PATCHes the card, then reveals the first secret", async () => {
    let body: unknown = null;
    renderApp("/cards/card-1", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.patch("*/api/v1/contracts/cards/:id", async ({ request }) => {
          body = await request.json();
          return HttpResponse.json({
            ...makeCard({ webhook_enabled: true }),
            webhook_secret: "whsec_first",
          });
        }),
        ...cardHandlers(makeCard()),
      ],
    });
    const { user, dialog } = await openEdit();
    expect(
      within(dialog).getByText(
        "Change how this card is described and how it is notified.",
      ),
    ).toBeVisible();
    expect(within(dialog).getByLabelText("Name")).toHaveValue(
      "demo-analytics-team",
    );
    expect(within(dialog).getByLabelText("Value")).toHaveValue("a@example.com");
    expect(
      within(dialog).queryByText(/A webhook can be configured/),
    ).toBeNull();

    await user.clear(within(dialog).getByLabelText("Name"));
    await user.clear(within(dialog).getByLabelText("Description"));
    await user.click(
      within(dialog).getByLabelText("Deliver notices to a webhook"),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Add contact" }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Save card" }));
    expect(
      await within(dialog).findByText("Enter a name for the card."),
    ).toBeVisible();
    expect(
      within(dialog).getByText("Describe who this card represents."),
    ).toBeVisible();
    expect(
      within(dialog).getByText(
        "Enter the webhook URL, or turn webhook delivery off.",
      ),
    ).toBeVisible();
    expect(
      within(dialog).getByText("Enter a value or remove this contact."),
    ).toBeVisible();
    expect(body).toBeNull();

    await user.click(
      within(dialog).getByRole("button", { name: "Remove contact 2" }),
    );
    expect(
      within(dialog).queryByText("Enter a value or remove this contact."),
    ).toBeNull();
    await user.type(within(dialog).getByLabelText("Name"), " ops ");
    await user.type(within(dialog).getByLabelText("Description"), "Ops team");
    await user.selectOptions(
      within(dialog).getByLabelText("Contact 1 kind"),
      "url",
    );
    await user.clear(within(dialog).getByLabelText(/Label/));
    await user.clear(within(dialog).getByLabelText("Value"));
    await user.type(
      within(dialog).getByLabelText("Value"),
      "https://wiki.example.com",
    );
    await user.click(
      within(dialog).getByLabelText("Email notices to the email contacts"),
    );
    await user.type(
      within(dialog).getByLabelText(/Webhook URL/),
      "https://hooks.example.com/x",
    );
    await user.click(within(dialog).getByRole("button", { name: "Save card" }));

    const reveal = await screen.findByRole("dialog", {
      name: /webhook secret/i,
    });
    expect(within(reveal).getByDisplayValue("whsec_first")).toBeVisible();
    expect(body).toEqual({
      name: "ops",
      description: "Ops team",
      contacts: [{ kind: "url", value: "https://wiki.example.com" }],
      email_enabled: true,
      webhook_enabled: true,
      webhook_url: "https://hooks.example.com/x",
    });
    expect(screen.queryByRole("dialog", { name: "Edit card" })).toBeNull();
  });

  it("saves without a webhook URL or secret and closes", async () => {
    let body: Record<string, unknown> | null = null;
    renderApp("/cards/card-1", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.patch("*/api/v1/contracts/cards/:id", async ({ request }) => {
          body = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(makeCard());
        }),
        ...cardHandlers(makeCard({ contacts: [] })),
      ],
    });
    const { user, dialog } = await openEdit();
    expect(
      within(dialog).getByText(
        "No contacts. Add who to reach about this card.",
      ),
    ).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Save card" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Edit card" })).toBeNull();
    });
    // The name is sent only when it changed, so a legacy stored name never blocks an edit.
    expect(body).toEqual({
      description: "Analytics team",
      contacts: [],
      email_enabled: false,
      webhook_enabled: false,
    });
    expect(
      screen.queryByRole("dialog", { name: /webhook secret/i }),
    ).toBeNull();
  });

  it("maps a refused webhook URL onto the field, then resets on Go back and Escape", async () => {
    renderApp("/cards/card-1", {
      folders,
      permissions: cardPermissions,
      handlers: [
        http.patch("*/api/v1/contracts/cards/:id", () =>
          problem(422, "Invalid request", {
            type: "urn:dc:problem:validation-error",
            errors: [
              {
                pointer: "/webhook_url",
                detail: "Private addresses are refused.",
              },
            ],
          }),
        ),
        ...cardHandlers(
          makeCard({
            webhook_enabled: true,
            webhook_url: "https://10.0.0.1/x",
          }),
        ),
      ],
    });
    const { user, dialog } = await openEdit();
    expect(within(dialog).getByLabelText(/Webhook URL/)).toHaveValue(
      "https://10.0.0.1/x",
    );
    await user.clear(within(dialog).getByLabelText("Name"));
    await user.type(within(dialog).getByLabelText("Name"), "renamed");
    await user.click(within(dialog).getByRole("button", { name: "Save card" }));
    expect(await within(dialog).findByText("Invalid request")).toBeVisible();
    // Once on the field and once in the alert's error list, labelled.
    expect(
      within(dialog).getAllByText(/Private addresses are refused\./),
    ).toHaveLength(2);
    expect(
      within(dialog).getByText(/Webhook URL/, { selector: "strong, span, b" }),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Edit card" })).toBeNull();
    });
    const again = await openEdit();
    expect(within(again.dialog).getByLabelText("Name")).toHaveValue(
      "demo-analytics-team",
    );
    expect(within(again.dialog).queryByText("Invalid request")).toBeNull();
    await user.clear(within(again.dialog).getByLabelText("Name"));
    await again.user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Edit card" })).toBeNull();
    });
    const third = await openEdit();
    expect(within(third.dialog).getByLabelText("Name")).toHaveValue(
      "demo-analytics-team",
    );
  });

  describe("a webhook host that is not on the allow-list", () => {
    function refuse(status: number, slug: string, detail: string) {
      return http.patch("*/api/v1/contracts/cards/:id", () =>
        HttpResponse.json(
          {
            type: `urn:dc:problem:${slug}`,
            title:
              status === 503 ? "Service Unavailable" : "Unprocessable Content",
            status,
            detail,
          },
          { status, headers: { "Content-Type": "application/problem+json" } },
        ),
      );
    }

    async function saveWith(handler: ReturnType<typeof refuse>, me?: Me) {
      renderApp("/cards/card-1", {
        folders,
        permissions: cardPermissions,
        ...(me ? { me } : {}),
        handlers: [handler, ...cardHandlers(makeCard())],
      });
      const { user, dialog } = await openEdit();
      await user.click(
        within(dialog).getByLabelText("Deliver notices to a webhook"),
      );
      await user.type(
        within(dialog).getByLabelText(/Webhook URL/),
        "https://hooks.example.org/x",
      );
      await user.click(
        within(dialog).getByRole("button", { name: "Save card" }),
      );
      return dialog;
    }

    const refused = refuse(
      422,
      "webhook-host-not-allowed",
      "This webhook host is not on the list of allowed hosts. A Global admin adds hosts in Settings > Notifications.",
    );

    it("tells a Global admin which list it is on and links to Settings", async () => {
      const dialog = await saveWith(
        refused,
        makeMe({
          global_permissions: ["settings:read", "folder:create", "user:read"],
        }),
      );
      expect(
        await within(dialog).findByText(
          "This host is not on the list of allowed webhook hosts.",
        ),
      ).toBeVisible();
      const link = within(dialog).getByRole("link", {
        name: "Add it in Settings",
      });
      expect(link).toHaveAttribute("href", "/admin/settings?tab=notifications");
      // The field carries the message: no alert repeats the problem.
      expect(within(dialog).queryByText("Unprocessable Content")).toBeNull();
      expect(within(dialog).queryByRole("status")).toBeNull();
    });

    it("tells anyone else to ask a Global admin, with no link", async () => {
      const dialog = await saveWith(
        refused,
        makeMe({ global_permissions: ["folder:create"] }),
      );
      expect(
        await within(dialog).findByText(
          "This host is not on the list of allowed webhook hosts.",
        ),
      ).toBeVisible();
      expect(
        within(dialog).getByText(
          "Ask a Global admin to add it in Administration > Settings > Notifications.",
        ),
      ).toBeVisible();
      expect(
        within(dialog).queryByRole("link", { name: "Add it in Settings" }),
      ).toBeNull();
    });

    it("shows a 503 settings-unavailable as a warning, never a red alert", async () => {
      const dialog = await saveWith(
        refuse(
          503,
          "settings-unavailable",
          "The webhook host settings cannot be read right now; try again shortly.",
        ),
      );
      const warning = await within(dialog).findByRole("status");
      expect(warning).toBeVisible();
      expect(within(dialog).queryByRole("alert")).toBeNull();
      expect(
        within(dialog).queryByText(/not on the list of allowed webhook hosts/),
      ).toBeNull();
    });
  });

  it("offers no Add contact once the card has twenty contacts", async () => {
    renderApp("/cards/card-1", {
      folders,
      permissions: cardPermissions,
      handlers: cardHandlers(
        makeCard({
          contacts: Array.from({ length: 20 }, (_, index) => ({
            kind: "text" as const,
            name: null,
            value: `contact ${String(index)}`,
          })),
        }),
      ),
    });
    const { dialog } = await openEdit();
    expect(within(dialog).getByLabelText("Contact 20 kind")).toHaveValue(
      "text",
    );
    expect(
      within(dialog).queryByRole("button", { name: "Add contact" }),
    ).toBeNull();
  });
});

describe("CardFormDialog (create)", () => {
  function capabilitiesBy(byFolder: Record<string, string[]>) {
    return http.get("*/api/v1/identity/me/capabilities", ({ request }) => {
      const folderId = new URL(request.url).searchParams.get("folder_id");
      return HttpResponse.json({
        folder_id: folderId,
        roles: [],
        permissions: byFolder[folderId ?? ""] ?? [],
        inheritance_blocked: false,
        grantable_roles: [],
      });
    });
  }

  it("refuses a chosen folder without card:create, then creates in an allowed one", async () => {
    const user = userEvent.setup();
    const capsSeen: string[] = [];
    let createdIn: unknown = null;
    const { router } = renderApp("/cards", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/me/capabilities", ({ request }) => {
          const folderId = new URL(request.url).searchParams.get("folder_id");
          capsSeen.push(folderId ?? "");
          const permissions =
            folderId === ROOT_ID || folderId === "risk-id"
              ? ["card:read", "card:create"]
              : ["card:read"];
          return HttpResponse.json({
            folder_id: folderId,
            roles: [],
            permissions,
            inheritance_blocked: false,
            grantable_roles: [],
          });
        }),
        http.get("*/api/v1/contracts/cards", () =>
          HttpResponse.json({
            items: [],
            next_cursor: null,
            total_cards: 0,
            total_folders: 0,
            pin_preview_limit: 5,
          }),
        ),
        http.post("*/api/v1/contracts/folders/:id/cards", ({ params }) => {
          createdIn = params.id;
          return HttpResponse.json(makeCard({ id: "card-9" }), { status: 201 });
        }),
        ...cardHandlers(makeCard({ id: "card-9" })),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create card" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    expect(
      within(dialog).getByText(
        "Choose the folder the card belongs to. Subscribe it to contract versions next.",
      ),
    ).toBeVisible();
    expect(
      within(dialog).getByText(
        "A webhook can be configured on the card after it is created.",
      ),
    ).toBeVisible();
    await user.selectOptions(
      within(dialog).getByLabelText("Folder"),
      "Acme / Fraud",
    );
    await waitFor(() => {
      expect(capsSeen).toContain("fraud-id");
    });
    expect(
      await within(dialog).findByText(
        "You cannot create cards in this folder.",
      ),
    ).toBeVisible();
    await user.type(within(dialog).getByLabelText("Name"), "ops");
    await user.type(within(dialog).getByLabelText("Description"), "Ops");
    await waitFor(async () => {
      await user.click(
        within(dialog).getByRole("button", { name: "Create card" }),
      );
      expect(
        within(dialog).getByText("You cannot create cards in this folder."),
      ).toBeVisible();
    });
    expect(createdIn).toBeNull();

    await user.selectOptions(
      within(dialog).getByLabelText("Folder"),
      "Acme / Risk",
    );
    await waitFor(() => {
      expect(capsSeen).toContain("risk-id");
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Create card" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/cards/card-9");
    });
    expect(createdIn).toBe("risk-id");
  });

  it("shows a create failure in the dialog and keeps it open", async () => {
    const user = userEvent.setup();
    renderApp("/folders/fraud-id/cards", {
      folders,
      handlers: [
        capabilitiesBy({
          [ROOT_ID]: ["card:read", "card:create"],
          "fraud-id": ["card:read", "card:create"],
        }),
        http.get("*/api/v1/contracts/cards", () =>
          HttpResponse.json({
            items: [],
            next_cursor: null,
            total_cards: 0,
            total_folders: 0,
            pin_preview_limit: 5,
          }),
        ),
        http.post("*/api/v1/contracts/folders/:id/cards", () =>
          problem(409, "Name taken", { detail: "A card named ops exists." }),
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create card" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    expect(
      within(dialog).getByText(
        "The card is created in Fraud. Subscribe it to contract versions next.",
      ),
    ).toBeVisible();
    await user.type(within(dialog).getByLabelText("Name"), "ops");
    await user.type(within(dialog).getByLabelText("Description"), "Ops");
    await user.click(
      within(dialog).getByRole("button", { name: "Create card" }),
    );
    expect(await within(dialog).findByText("Name taken")).toBeVisible();
    expect(within(dialog).getByText("A card named ops exists.")).toBeVisible();
  });
});

describe("the name rule on the card form", () => {
  it("shows the message for an emoji in the name and sends nothing", async () => {
    const user = userEvent.setup();
    let posted = false;
    renderApp("/cards", {
      folders,
      handlers: [
        http.get("*/api/v1/identity/me/capabilities", ({ request }) =>
          HttpResponse.json({
            folder_id: new URL(request.url).searchParams.get("folder_id"),
            roles: [],
            permissions: ["card:read", "card:create"],
            inheritance_blocked: false,
            grantable_roles: [],
          }),
        ),
        http.get("*/api/v1/contracts/cards", () =>
          HttpResponse.json({
            items: [],
            next_cursor: null,
            total_cards: 0,
            total_folders: 0,
            pin_preview_limit: 5,
          }),
        ),
        http.post("*/api/v1/contracts/folders/:id/cards", () => {
          posted = true;
          return HttpResponse.json({}, { status: 201 });
        }),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Create card" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create card" });
    await user.type(within(dialog).getByLabelText("Name"), "ops 😀");
    expect(
      within(dialog).getByText(
        "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.",
      ),
    ).toBeVisible();
    await user.type(within(dialog).getByLabelText("Description"), "Ops");
    await user.click(
      within(dialog).getByRole("button", { name: "Create card" }),
    );
    expect(posted).toBe(false);
  });
});
