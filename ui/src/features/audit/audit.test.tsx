// The audit explorer: the filter serialiser (RFC 3339, half-open range), the RFC 6902 change
// renderer, and the page through the real router (filters to query params, Load more, the
// detail drawer with the readable diff, the empty state).
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { AuditEvent } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { renderApp } from "@/test/renderApp";
import { describeChanges, resolvePointer } from "./changes";
import {
  fromLocalInput,
  presetRange,
  toApiQuery,
  toLocalInput,
  validateAuditSearch,
} from "./query";

afterEach(() => {
  tokenStore.clear();
});

function makeEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    event_id: "e-1",
    occurred_at: "2026-11-15T09:00:00Z",
    received_at: "2026-11-15T09:00:01Z",
    service: "contracts",
    category: "change",
    aggregate_type: "card",
    aggregate_id: "card-1",
    aggregate_version: 2,
    action: "card.updated",
    actor: { id: "local:u1", display: "Ada", via: "jwt" },
    request_id: "req-123456789",
    scope_path: [],
    before: { name: "old", tags: ["a"] },
    after: { name: "new", tags: ["a", "b"] },
    changes: [
      { op: "replace", path: "/name", value: "new" },
      { op: "add", path: "/tags/1", value: "b" },
    ],
    detail: null,
    ...overrides,
  };
}

describe("audit query serialiser", () => {
  it("keeps only valid params and drops junk", () => {
    expect(
      validateAuditSearch({
        category: "change",
        action: " contract.version. ",
        from: "not a date",
        to: "2026-11-15T10:00:00Z",
        extra: "x",
        aggregate_id: "",
      }),
    ).toEqual({
      category: "change",
      action: "contract.version.",
      to: "2026-11-15T10:00:00Z",
    });
    expect(validateAuditSearch({ category: "bogus" })).toEqual({});
  });

  it("serialises times as RFC 3339 UTC and omits unset filters", () => {
    expect(
      toApiQuery({
        actor_id: "local:u1",
        from: "2026-11-15T10:00:00+01:00",
        to: undefined,
      }),
    ).toEqual({ actor_id: "local:u1", from: "2026-11-15T09:00:00.000Z" });
  });

  it("round-trips local datetime inputs", () => {
    const utc = fromLocalInput("2026-11-15T10:30");
    expect(utc).toMatch(/Z$/);
    expect(toLocalInput(utc)).toBe("2026-11-15T10:30");
    expect(fromLocalInput("")).toBeUndefined();
  });

  it("builds open-ended presets", () => {
    expect(presetRange(3_600_000, Date.parse("2026-11-15T10:00:00Z"))).toEqual({
      from: "2026-11-15T09:00:00.000Z",
      to: undefined,
    });
  });
});

describe("RFC 6902 change renderer", () => {
  it("resolves escaped pointers", () => {
    expect(resolvePointer({ "a/b": { "c~d": 1 } }, "/a~1b/c~0d")).toBe(1);
  });

  it("pairs old values from before with new values", () => {
    const before = { name: "old", tags: ["a"], gone: true };
    expect(
      describeChanges(
        [
          { op: "replace", path: "/name", value: "new" },
          { op: "add", path: "/tags/1", value: "b" },
          { op: "remove", path: "/gone" },
        ],
        before,
      ),
    ).toEqual([
      {
        op: "replace",
        path: "/name",
        hasOld: true,
        oldValue: "old",
        hasNew: true,
        newValue: "new",
      },
      {
        op: "add",
        path: "/tags/1",
        hasOld: false,
        hasNew: true,
        newValue: "b",
      },
      {
        op: "remove",
        path: "/gone",
        hasOld: true,
        oldValue: true,
        hasNew: false,
      },
    ]);
  });
});

describe("audit explorer page", () => {
  it("applies filters to the API query and loads more", async () => {
    const user = userEvent.setup();
    const seen: URLSearchParams[] = [];
    const { router } = renderApp("/audit", {
      handlers: [
        http.get("*/api/v1/audit/events", ({ request }) => {
          const params = new URL(request.url).searchParams;
          seen.push(params);
          return HttpResponse.json(
            params.get("cursor") === null
              ? { items: [makeEvent()], next_cursor: "c-2" }
              : {
                  items: [
                    makeEvent({ event_id: "e-2", action: "card.created" }),
                  ],
                  next_cursor: null,
                },
          );
        }),
      ],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Audit log" }),
    ).toBeVisible();
    expect(await screen.findByText("card.updated")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(await screen.findByText("card.created")).toBeVisible();
    expect(seen[1]?.get("cursor")).toBe("c-2");

    await user.selectOptions(screen.getByLabelText("Category"), "denial");
    await user.click(screen.getByRole("button", { name: "Me" }));
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        category: "denial",
        actor_id: "local:11111111-1111-4111-8111-111111111111",
      });
    });
    await waitFor(() => {
      const last = seen.at(-1);
      expect(last?.get("category")).toBe("denial");
      expect(last?.get("actor_id")).toBe(
        "local:11111111-1111-4111-8111-111111111111",
      );
    });
  });

  it("opens the event drawer with the readable diff", async () => {
    const user = userEvent.setup();
    const { router } = renderApp(
      "/audit?aggregate_type=card&aggregate_id=card-1",
      {
        handlers: [
          http.get("*/api/v1/audit/events", ({ request }) => {
            const params = new URL(request.url).searchParams;
            expect(params.get("aggregate_type")).toBe("card");
            expect(params.get("aggregate_id")).toBe("card-1");
            return HttpResponse.json({
              items: [makeEvent()],
              next_cursor: null,
            });
          }),
          http.get("*/api/v1/audit/events/:id", () =>
            HttpResponse.json(makeEvent()),
          ),
        ],
      },
    );
    expect(screen.queryByText("card.updated")).toBeNull();
    const row = (await screen.findByText("card.updated")).closest("tr");
    await user.click(
      within(row as HTMLElement).getAllByRole("button")[0] as HTMLElement,
    );
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    expect(router.state.location.search).toMatchObject({ event: "e-1" });
    const changes = await within(drawer).findByRole("table");
    const rows = within(changes).getAllByRole("row");
    expect(within(rows[1] as HTMLElement).getByText("Changed")).toBeVisible();
    expect(within(rows[1] as HTMLElement).getByText("old")).toBeVisible();
    expect(within(rows[1] as HTMLElement).getByText("new")).toBeVisible();
    expect(within(rows[2] as HTMLElement).getByText("Added")).toBeVisible();
    expect(
      within(drawer).getByRole("region", { name: "Before snapshot" }),
    ).toHaveTextContent('"old"');
    expect(
      within(drawer).getByRole("button", { name: "Copy event JSON" }),
    ).toBeVisible();
  });

  it("shows the empty state without hinting at hidden events", async () => {
    renderApp("/audit?category=denial", {
      handlers: [
        http.get("*/api/v1/audit/events", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", {
        name: "No events you can see match these filters",
      }),
    ).toBeVisible();
    expect(
      screen.getAllByRole("button", { name: "Clear filters" }).length,
    ).toBeGreaterThan(0);
  });
});
