// The card's Subscriptions tab as a component: who pinned (the caller reads "You"), the contract
// name fallbacks, the deprecation badges, ended pins and their reasons, sorting, the state filter,
// unsubscribing, and a list answer without a body.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { describe, expect, it } from "vitest";
import {
  cardFixture,
  publishedVersionFixture,
  subscriptionFixture,
} from "@/test/cards-fixtures";
import { renderWithApi } from "@/test/governance-hooks";
import { sortRowsBy } from "@/test/governance-table";
import { http, HttpResponse, server } from "@/test/msw";
import { makeMe } from "@/test/renderApp";
import { CardSubscriptions } from "./CardSubscriptions";

const C = "*/api/v1/contracts";
const me = makeMe();

function mount(
  handlers: RequestHandler[],
  { canWrite = true, archived = false } = {},
) {
  server.use(
    http.get("*/api/v1/identity/me", () => HttpResponse.json(me)),
    http.post("*/api/v1/identity/principals/names", () =>
      HttpResponse.json({ items: [] }),
    ),
    // The (closed) Subscribe dialog prefetches the first contracts.
    http.get(`${C}/contracts`, () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    ),
    ...handlers,
  );
  renderWithApi(
    <CardSubscriptions
      card={cardFixture(
        archived ? { archived_at: "2026-11-05T10:00:00Z" } : {},
      )}
      canWrite={canWrite}
    />,
  );
}

const ordersPin = subscriptionFixture({ pinned_by: me.principal_id });
const paymentsPin = subscriptionFixture({
  id: "sub-2",
  contract_id: "contract-2",
  version_id: "v-9",
  semver: "2.0.0",
  pinned_at: "2026-11-01T10:00:00Z",
});
const hiddenPin = subscriptionFixture({
  id: "sub-3",
  contract_id: "contract-3",
  version_id: "v-5",
  semver: "3.0.0",
  pinned_at: "2026-11-03T10:00:00Z",
});

const contractHandlers: RequestHandler[] = [
  http.get(`${C}/contracts/contract-1`, () =>
    HttpResponse.json({ name: null, odcs_id: "orders-odcs" }),
  ),
  http.get(`${C}/contracts/contract-2`, () =>
    HttpResponse.json({ name: "Payments", odcs_id: "payments-odcs" }),
  ),
  // A contract the caller may not read any more: its id stands in for the name.
  http.get(
    `${C}/contracts/contract-3`,
    () => new HttpResponse(null, { status: 204 }),
  ),
  http.get(`${C}/contracts/contract-1/versions`, () =>
    HttpResponse.json({
      items: [
        publishedVersionFixture("1.0.0", "v-1", {
          deprecated: true,
          sunset_date: "2027-01-31",
        }),
      ],
    }),
  ),
  http.get(`${C}/contracts/contract-2/versions`, () =>
    HttpResponse.json({
      items: [publishedVersionFixture("2.0.0", "v-9", { deprecated: true })],
    }),
  ),
  http.get(`${C}/contracts/contract-3/versions`, () =>
    HttpResponse.json({ items: [] }),
  ),
];

describe("card subscriptions tab", () => {
  it("names contracts and pinners, badges deprecated pins and sorts by pin date", async () => {
    const user = userEvent.setup();
    mount([
      http.get(`${C}/cards/card-1/subscriptions`, () =>
        HttpResponse.json({
          items: [ordersPin, paymentsPin, hiddenPin],
          next_cursor: null,
        }),
      ),
      ...contractHandlers,
    ]);
    const table = await screen.findByRole("table", {
      name: "Subscriptions of demo-analytics-team",
    });
    expect(
      await within(table).findByRole("link", { name: "orders-odcs" }),
    ).toBeVisible();
    expect(
      await within(table).findByRole("link", { name: "Payments" }),
    ).toBeVisible();
    expect(
      within(table).getByRole("link", { name: "contract-3" }),
    ).toBeVisible();
    expect(
      await within(table).findByText("Deprecated, sunset 2027-01-31"),
    ).toBeVisible();
    expect(await within(table).findByText("Deprecated")).toBeVisible();
    expect(within(table).getByText("You")).toBeVisible();
    expect(await sortRowsBy(user, table, "Pinned at")).toEqual([
      "Payments",
      "orders-odcs",
      "contract-3",
    ]);
  });

  it("shows ended pins with their reason and no actions", async () => {
    const user = userEvent.setup();
    const states: (string | null)[] = [];
    mount([
      http.get(`${C}/cards/card-1/subscriptions`, ({ request }) => {
        const state = new URL(request.url).searchParams.get("state");
        states.push(state);
        return HttpResponse.json({
          items:
            state === "ended"
              ? [
                  subscriptionFixture({
                    ended_at: "2026-11-04T10:00:00Z",
                    end_reason: "moved",
                  }),
                  subscriptionFixture({
                    id: "sub-4",
                    semver: "0.9.0",
                    ended_at: "2026-11-04T10:00:00Z",
                    end_reason: null,
                  }),
                ]
              : [],
          next_cursor: null,
        });
      }),
      ...contractHandlers,
    ]);
    expect(await screen.findByText("No active subscriptions")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Show"), "ended");
    const table = await screen.findByRole("table");
    expect(within(table).getByText(/^Moved /)).toBeVisible();
    expect(within(table).getByText(/^Ended /)).toBeVisible();
    expect(
      within(table).queryByRole("button", { name: /^(Move pin|Unsubscribe)/ }),
    ).toBeNull();
    await user.selectOptions(screen.getByLabelText("Show"), "all");
    expect(await screen.findByText("No pins here")).toBeVisible();
    expect(states).toEqual(["active", "ended", "all"]);
  });

  it("unsubscribes an active pin after confirmation", async () => {
    const user = userEvent.setup();
    const ended: string[] = [];
    mount([
      http.get(`${C}/cards/card-1/subscriptions`, () =>
        HttpResponse.json({ items: [paymentsPin], next_cursor: null }),
      ),
      http.post(
        `${C}/cards/card-1/subscriptions/:id/unsubscribe`,
        ({ params }) => {
          ended.push(String(params.id));
          return new HttpResponse(null, { status: 204 });
        },
      ),
      ...contractHandlers,
    ]);
    await user.click(
      await screen.findByRole("button", { name: "Unsubscribe from 2.0.0" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Unsubscribe?",
    });
    expect(dialog).toHaveTextContent(
      "demo-analytics-team stops receiving notices for version 2.0.0.",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Unsubscribe card" }),
    );
    await waitFor(() => {
      expect(ended).toEqual(["sub-2"]);
    });
  });

  it("offers no subscribe or pin actions on an archived card", async () => {
    mount(
      [
        http.get(`${C}/cards/card-1/subscriptions`, () =>
          HttpResponse.json({ items: [paymentsPin], next_cursor: null }),
        ),
        ...contractHandlers,
      ],
      { archived: true },
    );
    expect(await screen.findByRole("table")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Move pin/ })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Subscribe to a contract" }),
    ).toBeNull();
  });

  it("treats a list answer without a body as a failure", async () => {
    mount([
      http.get(
        `${C}/cards/card-1/subscriptions`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    ]);
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });
});
