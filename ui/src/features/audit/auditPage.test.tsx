// The audit explorer through the real router: the list states (error with retry, empty with and
// without filters, a failed later page), every object link the table renders, the filter bar
// (each field, presets, Clear filters, a URL change resetting the draft) and the request each
// action sends.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AuditEvent } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeMe, renderApp } from "@/test/renderApp";

vi.setConfig({ testTimeout: 60000 });

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
    before: null,
    after: null,
    changes: [],
    detail: null,
    ...overrides,
  };
}

const problem = (status: number, title: string) =>
  HttpResponse.json(
    { type: "urn:dc:problem:internal", title, status },
    {
      status,
      headers: { "Content-Type": "application/problem+json" },
    },
  );

function listing(items: AuditEvent[], seen: URLSearchParams[] = []) {
  return http.get("*/api/v1/audit/events", ({ request }) => {
    seen.push(new URL(request.url).searchParams);
    return HttpResponse.json({ items, next_cursor: null });
  });
}

describe("audit list states", () => {
  it("shows the API problem and retries the first page", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/audit", {
      handlers: [
        http.get("*/api/v1/audit/events", () => {
          calls += 1;
          return calls === 1
            ? problem(500, "Audit is down")
            : HttpResponse.json({
                items: [makeEvent()],
                next_cursor: null,
              });
        }),
      ],
    });
    expect(await screen.findByText("Audit is down")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(await screen.findByText("card.updated")).toBeVisible();
    expect(screen.queryByText("Audit is down")).toBeNull();
    expect(calls).toBe(2);
  });

  it("asks for 50 events with no filter and shows the unfiltered empty state", async () => {
    const seen: URLSearchParams[] = [];
    renderApp("/audit", { handlers: [listing([], seen)] });
    expect(
      await screen.findByRole("heading", {
        name: "No events you can see match these filters",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Events appear here as people work with folders, contracts and rules.",
      ),
    ).toBeVisible();
    // Neither the empty state nor the filter bar offers Clear filters without a filter.
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
    expect([...(seen[0]?.entries() ?? [])]).toEqual([["limit", "50"]]);
  });

  it("clears the filters from the empty state", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/audit?service=identity&action=user.", {
      handlers: [listing([])],
    });
    expect(
      await screen.findByText("Widen the time range or clear a filter."),
    ).toBeVisible();
    const buttons = screen.getAllByRole("button", { name: "Clear filters" });
    expect(buttons).toHaveLength(2);
    // The empty state's own action (the filter bar's comes first in the document).
    await user.click(buttons[1] as HTMLElement);
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
  });

  it("keeps loaded rows and shows the problem when a later page fails", async () => {
    const user = userEvent.setup();
    renderApp("/audit", {
      handlers: [
        http.get("*/api/v1/audit/events", ({ request }) =>
          new URL(request.url).searchParams.get("cursor") === null
            ? HttpResponse.json({ items: [makeEvent()], next_cursor: "c-2" })
            : problem(500, "Page two failed"),
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Load more results" }),
    );
    expect(await screen.findByText("Page two failed")).toBeVisible();
    expect(screen.getByText("card.updated")).toBeVisible();
  });
});

describe("audit table cells", () => {
  const linked: [string, string, string][] = [
    ["contract", "c-1", "/contracts/c-1"],
    ["folder", "f-1", "/folders/f-1"],
    ["card", "k-1", "/cards/k-1"],
    ["rule_set", "rs-1", "/rule-sets/rs-1"],
    ["template", "t-1", "/templates/t-1"],
    ["group", "g-1", "/admin/groups/g-1"],
    ["user", "u-1", "/admin/users/u-1"],
    ["service_account", "sa-1", "/admin/service-accounts/sa-1"],
    ["entra_user", "eu-1", "/admin/entra/users/eu-1"],
  ];

  it("links each object type to its page and leaves the rest as text", async () => {
    const events = [
      ...linked.map(([type, id], index) =>
        makeEvent({
          event_id: `e-${String(index)}`,
          aggregate_type: type,
          aggregate_id: id,
          action: `${type}.updated`,
        }),
      ),
      makeEvent({
        event_id: "e-deleted",
        aggregate_type: "contract",
        aggregate_id: "c-gone",
        action: "contract.deleted",
      }),
      makeEvent({
        event_id: "e-reg-group",
        aggregate_type: "entra_registration",
        aggregate_id: "reg-g",
        action: "entra_registration.created",
        after: { kind: "group" },
      }),
      makeEvent({
        event_id: "e-reg-sp",
        aggregate_type: "entra_registration",
        aggregate_id: "reg-sp",
        action: "entra_registration.deactivated",
        before: { kind: "service_principal" },
      }),
      makeEvent({
        event_id: "e-reg-user",
        aggregate_type: "entra_registration",
        aggregate_id: "reg-u",
        action: "entra_registration.created",
        after: { kind: "user" },
      }),
      makeEvent({
        event_id: "e-grant",
        aggregate_type: "grant",
        aggregate_id: "gr-1",
        action: "grant.created",
        after: { folder_id: "f-9" },
      }),
      makeEvent({
        event_id: "e-grant-bare",
        aggregate_type: "grant",
        aggregate_id: "gr-2",
        action: "grant.revoked",
      }),
      makeEvent({
        event_id: "e-system",
        aggregate_type: "system",
        aggregate_id: "abcdefgh-1234-5678-wxyz",
        action: "system.started",
      }),
    ];
    renderApp("/audit", { handlers: [listing(events)] });
    await screen.findByText("system.started");
    for (const [type, id, href] of linked) {
      const cell = screen.getByTitle(`${type} ${id}`);
      expect(cell.tagName).toBe("A");
      expect(cell).toHaveAttribute("href", href);
      expect(cell).toHaveTextContent(`${type} ${id}`);
    }
    // Entra registrations link by kind; a grant opens its folder's Access tab.
    const snapshotLinks: [string, string][] = [
      ["entra_registration reg-g", "/admin/entra/groups/reg-g"],
      ["entra_registration reg-sp", "/admin/entra/service-principals/reg-sp"],
      ["grant gr-1", "/folders/f-9/access"],
    ];
    for (const [title, href] of snapshotLinks)
      expect(screen.getByTitle(title)).toHaveAttribute("href", href);
    expect(screen.getByTitle("entra_registration reg-u").tagName).toBe("SPAN");
    expect(screen.getByTitle("grant gr-2").tagName).toBe("SPAN");
    // A deleted contract has no page to open.
    const deleted = screen.getByTitle("contract c-gone");
    expect(deleted.tagName).toBe("SPAN");
    // An unknown type is text, and a long id is shortened (the title keeps it whole).
    const system = screen.getByTitle("system abcdefgh-1234-5678-wxyz");
    expect(system.tagName).toBe("SPAN");
    expect(system).toHaveTextContent("system abcdefgh…wxyz");
  });

  it("renders actor fallbacks, categories and unparseable times", async () => {
    renderApp("/audit", {
      handlers: [
        listing([
          makeEvent({
            event_id: "e-a",
            action: "first.action",
            category: "denial",
            actor: { id: "local:u9", display: "", via: 3 },
            request_id: null,
          }),
          makeEvent({
            event_id: "e-b",
            action: "second.action",
            category: "custom",
            actor: {},
            occurred_at: "not-a-time",
          }),
        ]),
      ],
    });
    const first = (await screen.findByText("first.action")).closest("tr");
    const second = screen.getByText("second.action").closest("tr");
    expect(first).not.toBeNull();
    expect(second).not.toBeNull();
    const rowA = within(first as HTMLElement);
    const rowB = within(second as HTMLElement);
    // No display name: the id is the name; a non-string via is left out.
    expect(rowA.getByTitle("local:u9")).toHaveTextContent(/^local:u9$/);
    expect(rowA.getByText("Denial")).toBeVisible();
    // No actor at all: "System".
    expect(rowB.getByText("System")).toBeVisible();
    expect(rowB.getByText("Custom")).toBeVisible();
    expect(rowB.getByText("not-a-time")).toBeVisible();
    expect(rowB.getByRole("button", { name: "not-a-time" })).toBeVisible();
  });

  it("opens the drawer from the time cell", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/audit?service=contracts", {
      handlers: [
        listing([makeEvent()]),
        http.get("*/api/v1/audit/events/:id", () =>
          HttpResponse.json(makeEvent()),
        ),
      ],
    });
    await user.click(await screen.findByTitle("2026-11-15T09:00:00Z (UTC)"));
    await screen.findByRole("dialog", { name: "Audit event" });
    expect(router.state.location.search).toEqual({
      service: "contracts",
      event: "e-1",
    });
  });
});

describe("audit filter bar", () => {
  it("sends every text and select field to the URL and the API", async () => {
    const user = userEvent.setup();
    const seen: URLSearchParams[] = [];
    const { router } = renderApp("/audit", {
      handlers: [listing([], seen)],
    });
    await screen.findByRole("heading", {
      name: "No events you can see match these filters",
    });
    const form = screen.getByRole("search", { name: "Audit filters" });
    const bar = within(form);
    await user.type(bar.getByLabelText("Object type"), "  contract ");
    await user.type(bar.getByLabelText("Object id"), "c-1");
    await user.type(bar.getByLabelText("Actor id"), "entra:x");
    await user.selectOptions(bar.getByLabelText("Service"), "validation");
    await user.selectOptions(bar.getByLabelText("Category"), "access");
    await user.type(bar.getByLabelText("Action"), "contract.version.");
    fireEvent.change(bar.getByLabelText("From (local time)"), {
      target: { value: "2026-11-01T08:00" },
    });
    fireEvent.change(bar.getByLabelText("To (local time)"), {
      target: { value: "2026-11-02T08:00" },
    });
    await user.click(bar.getByRole("button", { name: "Apply filters" }));
    const from = new Date(2026, 10, 1, 8, 0).toISOString();
    const to = new Date(2026, 10, 2, 8, 0).toISOString();
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        aggregate_type: "contract",
        aggregate_id: "c-1",
        actor_id: "entra:x",
        service: "validation",
        category: "access",
        action: "contract.version.",
        from,
        to,
      });
    });
    await waitFor(() => {
      expect(Object.fromEntries(seen.at(-1)?.entries() ?? [])).toEqual({
        aggregate_type: "contract",
        aggregate_id: "c-1",
        actor_id: "entra:x",
        service: "validation",
        category: "access",
        action: "contract.version.",
        from,
        to,
        limit: "50",
      });
    });
    // Applied filters bring the filter bar's Clear filters.
    await user.click(bar.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    // The URL changed under the form: the draft follows it.
    expect(bar.getByLabelText("Object type")).toHaveValue("");
    expect(bar.getByLabelText("From (local time)")).toHaveValue("");
  });

  it("applies a time preset that keeps the draft and leaves the range open", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/audit", { handlers: [listing([])] });
    const form = await screen.findByRole("search", { name: "Audit filters" });
    const bar = within(form);
    await user.type(bar.getByLabelText("Action"), "folder.");
    const before = Date.now();
    await user.click(bar.getByRole("button", { name: "Last 24 hours" }));
    await waitFor(() => {
      expect(router.state.location.search).toMatchObject({
        action: "folder.",
      });
    });
    const search = router.state.location.search as Record<string, string>;
    expect(search.to).toBeUndefined();
    const from = Date.parse(search.from ?? "");
    expect(from).toBeGreaterThanOrEqual(before - 86_400_000 - 1000);
    expect(from).toBeLessThanOrEqual(Date.now() - 86_400_000);
    expect(bar.getByLabelText("From (local time)")).not.toHaveValue("");
    expect(
      within(bar.getByRole("group", { name: "Time presets" })).getAllByRole(
        "button",
      ),
    ).toHaveLength(4);
  });

  it("shows the applied filters from a deep link and follows a URL change", async () => {
    const { router } = renderApp(
      "/audit?actor_id=local:u1&from=2026-11-01T08:00:00.000Z",
      { handlers: [listing([])] },
    );
    const form = await screen.findByRole("search", { name: "Audit filters" });
    const bar = within(form);
    expect(bar.getByLabelText("Actor id")).toHaveValue("local:u1");
    expect(bar.getByLabelText("From (local time)")).toHaveValue(
      (() => {
        const d = new Date("2026-11-01T08:00:00.000Z");
        const p = (n: number) => String(n).padStart(2, "0");
        return `${String(d.getFullYear())}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
      })(),
    );
    await router.navigate({ to: "/audit", search: { service: "audit" } });
    await waitFor(() => {
      expect(bar.getByLabelText("Service")).toHaveValue("audit");
    });
    expect(bar.getByLabelText("Actor id")).toHaveValue("");
  });

  it("offers no Me shortcut without a principal id", async () => {
    renderApp("/audit", {
      me: makeMe({ principal_id: "" }),
      handlers: [listing([])],
    });
    const form = await screen.findByRole("search", { name: "Audit filters" });
    expect(within(form).getByLabelText("Actor id")).toBeVisible();
    expect(within(form).queryByRole("button", { name: "Me" })).toBeNull();
  });
});

describe("audit responses without a body", () => {
  it("shows the generic problem for a list and an event answered with 204", async () => {
    renderApp("/audit?event=e-1", {
      handlers: [
        http.get(
          "*/api/v1/audit/events",
          () => new HttpResponse(null, { status: 204 }),
        ),
        http.get(
          "*/api/v1/audit/events/:id",
          () => new HttpResponse(null, { status: 204 }),
        ),
      ],
    });
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    expect(
      await within(drawer).findByText("Something went wrong"),
    ).toBeVisible();
    await waitFor(() => {
      expect(screen.getAllByText("Something went wrong")).toHaveLength(2);
    });
  });
});
