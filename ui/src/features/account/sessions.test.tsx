// The Sessions card on /account/profile (quick 261120-s0p): one row per live session with the
// current one badged, an unknown device, the Microsoft-managed note, and the failed and loading
// states. msw stands in for the gateway.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeMe, renderApp } from "@/test/renderApp";

afterEach(() => {
  tokenStore.clear();
});

const SLOW = { timeout: 10000 };
const SESSIONS = "*/api/v1/identity/me/sessions";

type Item = {
  id: string;
  started_at: string;
  last_active_at: string;
  idle_expires_at: string;
  expires_at: string;
  client: string | null;
  current: boolean;
};

function item(overrides: Partial<Item> & Pick<Item, "id">): Item {
  return {
    started_at: "2026-11-20T08:00:00Z",
    last_active_at: "2026-11-20T09:30:00Z",
    idle_expires_at: "2026-11-27T09:30:00Z",
    expires_at: "2026-11-27T08:00:00Z",
    client: "Chrome on macOS",
    current: false,
    ...overrides,
  };
}

function list(items: Item[], extra: Record<string, unknown> = {}) {
  return HttpResponse.json({
    items,
    total: items.length,
    max_sessions: 5,
    access_token_lifetime_s: 900,
    ...extra,
  });
}

describe("sessions card", () => {
  it("lists a session per row with the current one badged and the footer count", async () => {
    renderApp("/account/profile", {
      handlers: [
        http.get(SESSIONS, () =>
          list([
            item({ id: "s-1", current: true }),
            item({ id: "s-2", client: "API client (curl)" }),
            item({ id: "s-3", client: null }),
          ]),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { level: 2, name: "Sessions" }, SLOW),
    ).toBeVisible();
    const table = await screen.findByRole("table", {}, SLOW);
    const body = within(table).getAllByRole("row").slice(1);
    expect(body).toHaveLength(3);
    const [first, second, third] = body as [
      HTMLElement,
      HTMLElement,
      HTMLElement,
    ];
    expect(within(first).getByText("Chrome on macOS")).toBeVisible();
    expect(within(first).getByText("This session")).toBeVisible();
    expect(within(second).getByText("API client (curl)")).toBeVisible();
    expect(within(second).queryByText("This session")).toBeNull();
    expect(within(third).getByText("Unknown device")).toBeVisible();
    for (const header of ["Device", "Signed in", "Last active", "Ends"]) {
      expect(
        within(table).getByRole("columnheader", { name: header }),
      ).toBeVisible();
    }
    // Dates go through formatDateTime, never the raw ISO string.
    expect(screen.queryByText(/2026-11-20T/)).toBeNull();
    expect(
      screen.getByText(
        "3 of at most 5 sessions. Signing in beyond that ends the oldest one.",
      ),
    ).toBeVisible();
    // Session ids are never shown.
    expect(screen.queryByText("s-1")).toBeNull();
  });

  it("shows the empty state when no session is live", async () => {
    renderApp("/account/profile");
    expect(
      await screen.findByText("No active sessions", {}, SLOW),
    ).toBeVisible();
  });

  it("tells a Microsoft Entra account that Entra manages its sessions and requests nothing", async () => {
    let requested = false;
    renderApp("/account/profile", {
      me: makeMe({ idp: "entra" }),
      handlers: [
        http.get(SESSIONS, () => {
          requested = true;
          return list([]);
        }),
      ],
    });
    expect(
      await screen.findByText(
        "Microsoft Entra ID manages your sign-in sessions.",
        {},
        SLOW,
      ),
    ).toBeVisible();
    expect(screen.queryByRole("table")).toBeNull();
    expect(requested).toBe(false);
  });

  it("renders a failed list as a warning alert with Retry loading", async () => {
    renderApp("/account/profile", {
      handlers: [
        http.get(SESSIONS, () =>
          HttpResponse.json(
            {
              type: "urn:dc:problem:service-unavailable",
              title: "Service Unavailable",
              status: 503,
              detail: "Try again shortly.",
            },
            {
              status: 503,
              headers: { "Content-Type": "application/problem+json" },
            },
          ),
        ),
      ],
    });
    expect(
      await screen.findByRole("button", { name: "Retry loading" }, SLOW),
    ).toBeVisible();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("shows a skeleton while the list loads", async () => {
    renderApp("/account/profile", {
      handlers: [http.get(SESSIONS, () => new Promise<never>(() => {}))],
    });
    expect(await screen.findByText("Loading sessions", {}, SLOW)).toBeVisible();
  });
});

// --- signing sessions out ----------------------------------------------------------------------

function problemResponse(status: number, slug: string, detail: string) {
  return HttpResponse.json(
    { type: `urn:dc:problem:${slug}`, title: "Refused", status, detail },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

function selfHandlers(rows: Item[]) {
  let current = rows;
  const calls: { method: string; path: string }[] = [];
  const handlers = [
    http.get(SESSIONS, () => list(current)),
    http.delete(`${SESSIONS}/:id`, ({ params }) => {
      calls.push({ method: "DELETE", path: String(params.id) });
      current = current.filter((row) => row.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
    http.post(`${SESSIONS}/revoke-others`, () => {
      calls.push({ method: "POST", path: "revoke-others" });
      const revoked = current.filter((row) => !row.current).length;
      current = current.filter((row) => row.current);
      return HttpResponse.json({ revoked });
    }),
  ];
  return { handlers, calls };
}

describe("signing sessions out (self)", () => {
  it("signs one session out after a confirmation and refreshes the list", async () => {
    const actor = userEvent.setup();
    const { handlers, calls } = selfHandlers([
      item({ id: "s-1", current: true }),
      item({ id: "s-2", client: "Firefox on Linux" }),
    ]);
    renderApp("/account/profile", { handlers });
    const row = (await screen.findByText("Firefox on Linux", {}, SLOW)).closest(
      "tr",
    ) as HTMLElement;
    // the current session has no row action
    const currentRow = screen
      .getByText("This session")
      .closest("tr") as HTMLElement;
    expect(
      within(currentRow).queryByRole("button", { name: /Sign out/ }),
    ).toBeNull();
    await actor.click(within(row).getByRole("button", { name: /Sign out/ }));
    const dialog = await screen.findByRole("alertdialog");
    expect(
      within(dialog).getByRole("heading", { name: "Sign out this session?" }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Keep session" }),
    ).toBeVisible();
    await actor.click(
      within(dialog).getByRole("button", { name: "Sign out session" }),
    );
    await waitFor(() => {
      expect(calls).toEqual([{ method: "DELETE", path: "s-2" }]);
    });
    expect(await screen.findByText("Session signed out.")).toBeVisible();
    await waitFor(() => {
      expect(screen.queryByText("Firefox on Linux")).toBeNull();
    });
  });

  it("keeps the session when the confirmation is dismissed", async () => {
    const actor = userEvent.setup();
    const { handlers, calls } = selfHandlers([
      item({ id: "s-1", current: true }),
      item({ id: "s-2", client: "Firefox on Linux" }),
    ]);
    renderApp("/account/profile", { handlers });
    const row = (await screen.findByText("Firefox on Linux", {}, SLOW)).closest(
      "tr",
    ) as HTMLElement;
    await actor.click(within(row).getByRole("button", { name: /Sign out/ }));
    const dialog = await screen.findByRole("alertdialog");
    await actor.click(
      within(dialog).getByRole("button", { name: "Keep session" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(calls).toEqual([]);
  });

  it("keeps the dialog open with the problem when a sign-out fails", async () => {
    const actor = userEvent.setup();
    renderApp("/account/profile", {
      handlers: [
        http.get(SESSIONS, () =>
          list([
            item({ id: "s-1", current: true }),
            item({ id: "s-2", client: "Firefox on Linux" }),
          ]),
        ),
        http.delete(`${SESSIONS}/:id`, () =>
          problemResponse(409, "conflict", "That session cannot be ended."),
        ),
      ],
    });
    const row = (await screen.findByText("Firefox on Linux", {}, SLOW)).closest(
      "tr",
    ) as HTMLElement;
    await actor.click(within(row).getByRole("button", { name: /Sign out/ }));
    const dialog = await screen.findByRole("alertdialog");
    await actor.click(
      within(dialog).getByRole("button", { name: "Sign out session" }),
    );
    expect(await within(dialog).findByRole("alert")).toBeVisible();
    expect(screen.getByRole("alertdialog")).toBeVisible();
  });

  it("signs out everywhere else and says how many ended", async () => {
    const actor = userEvent.setup();
    const { handlers, calls } = selfHandlers([
      item({ id: "s-1", current: true }),
      item({ id: "s-2", client: "Firefox on Linux" }),
      item({ id: "s-3", client: "API client (curl)" }),
    ]);
    renderApp("/account/profile", { handlers });
    await actor.click(
      await screen.findByRole(
        "button",
        { name: "Sign out everywhere else" },
        SLOW,
      ),
    );
    const dialog = await screen.findByRole("alertdialog");
    expect(
      within(dialog).getByRole("heading", {
        name: "Sign out every other session?",
      }),
    ).toBeVisible();
    expect(
      within(dialog).getByText(
        "Every session except this one ends at its next refresh.",
      ),
    ).toBeVisible();
    await actor.click(
      within(dialog).getByRole("button", { name: "Sign out everywhere else" }),
    );
    await waitFor(() => {
      expect(calls).toEqual([{ method: "POST", path: "revoke-others" }]);
    });
    expect(
      await screen.findByText("Signed out 2 other sessions."),
    ).toBeVisible();
    await waitFor(() => {
      expect(screen.queryByText("Firefox on Linux")).toBeNull();
    });
    // nothing else to sign out: the action is gone
    expect(
      screen.queryByRole("button", { name: "Sign out everywhere else" }),
    ).toBeNull();
  });

  it("explains the access token lifetime under the table", async () => {
    renderApp("/account/profile", {
      handlers: [
        http.get(SESSIONS, () =>
          list([item({ id: "s-1", current: true })], {
            access_token_lifetime_s: 900,
          }),
        ),
      ],
    });
    expect(
      await screen.findByText(
        "A signed-out session ends at its next refresh; it keeps the access it already has for up to 15 minutes.",
        {},
        SLOW,
      ),
    ).toBeVisible();
    // only the current session: no sign-out controls at all
    expect(
      screen.queryByRole("button", { name: "Sign out everywhere else" }),
    ).toBeNull();
  });

  it("hides every sign-out control from a non-interactive credential", async () => {
    renderApp("/account/profile", {
      me: makeMe({ interactive: false, via: "pat" }),
      handlers: [
        http.get(SESSIONS, () =>
          list([
            item({ id: "s-1", client: "Chrome on macOS" }),
            item({ id: "s-2", client: "Firefox on Linux" }),
          ]),
        ),
      ],
    });
    expect(await screen.findByText("Firefox on Linux", {}, SLOW)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Sign out/ })).toBeNull();
  });
});

describe("sessions card, more states", () => {
  const rowNames = (table: HTMLElement) =>
    within(table)
      .getAllByRole("rowheader")
      .map((cell) => cell.textContent);

  it("sorts by the sign-in, last-active and end times", async () => {
    const actor = userEvent.setup();
    renderApp("/account/profile", {
      handlers: [
        http.get(SESSIONS, () =>
          list([
            item({
              id: "s-1",
              client: "Early",
              started_at: "2026-11-18T08:00:00Z",
              last_active_at: "2026-11-20T10:00:00Z",
              expires_at: "2026-11-25T08:00:00Z",
            }),
            item({
              id: "s-2",
              client: "Late",
              started_at: "2026-11-19T08:00:00Z",
              last_active_at: "2026-11-19T10:00:00Z",
              expires_at: "2026-11-26T08:00:00Z",
            }),
          ]),
        ),
      ],
    });
    const table = await screen.findByRole("table", {}, SLOW);
    await actor.click(within(table).getByRole("button", { name: "Signed in" }));
    expect(rowNames(table)).toEqual(["Early", "Late"]);
    await actor.click(
      within(table).getByRole("button", { name: "Last active" }),
    );
    expect(rowNames(table)).toEqual(["Late", "Early"]);
    await actor.click(within(table).getByRole("button", { name: "Ends" }));
    expect(rowNames(table)).toEqual(["Early", "Late"]);
  });

  it("loads the list again from Retry loading", async () => {
    const actor = userEvent.setup();
    let fail = true;
    renderApp("/account/profile", {
      handlers: [
        http.get(SESSIONS, () =>
          fail
            ? problemResponse(503, "service-unavailable", "Down.")
            : list([item({ id: "s-1", current: true })]),
        ),
      ],
    });
    const retry = await screen.findByRole(
      "button",
      { name: "Retry loading" },
      SLOW,
    );
    fail = false;
    await actor.click(retry);
    expect(await screen.findByText("This session", {}, SLOW)).toBeVisible();
  });

  it("refuses a list answer without a body", async () => {
    renderApp("/account/profile", {
      handlers: [
        http.get(SESSIONS, () => new HttpResponse(null, { status: 204 })),
      ],
    });
    expect(
      await screen.findByText("Something went wrong", {}, SLOW),
    ).toBeVisible();
  });

  it("says one other session ended, and zero when the answer has no count", async () => {
    const actor = userEvent.setup();
    let answer: () => Response = () => HttpResponse.json({ revoked: 1 });
    renderApp("/account/profile", {
      handlers: [
        http.get(SESSIONS, () =>
          list([
            item({ id: "s-1", current: true }),
            item({ id: "s-2", client: "Firefox on Linux" }),
          ]),
        ),
        http.post(`${SESSIONS}/revoke-others`, () => answer()),
      ],
    });
    const signOutOthers = async () => {
      await actor.click(
        await screen.findByRole(
          "button",
          { name: "Sign out everywhere else" },
          SLOW,
        ),
      );
      const dialog = await screen.findByRole("alertdialog");
      await actor.click(
        within(dialog).getByRole("button", {
          name: "Sign out everywhere else",
        }),
      );
    };
    await signOutOthers();
    expect(
      await screen.findByText("Signed out 1 other session."),
    ).toBeVisible();
    answer = () => new HttpResponse(null, { status: 204 });
    await signOutOthers();
    expect(
      await screen.findByText("Signed out 0 other sessions."),
    ).toBeVisible();
  });
});
