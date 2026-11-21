// The audit event drawer through a deep link (`/audit?event=`): the GET it sends, the envelope
// facts and their fallbacks, every change-row variant, the snapshots, the detail pairs, the
// skeleton, Not found, a problem with retry, and closing back to the filters.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AuditEvent } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { renderApp } from "@/test/renderApp";

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

const emptyList = http.get("*/api/v1/audit/events", () =>
  HttpResponse.json({ items: [], next_cursor: null }),
);

function openAt(path: string, event: AuditEvent | (() => Response)) {
  const requested: string[] = [];
  const result = renderApp(path, {
    handlers: [
      emptyList,
      http.get("*/api/v1/audit/events/:id", ({ params }) => {
        requested.push(String(params.id));
        return typeof event === "function" ? event() : HttpResponse.json(event);
      }),
    ],
  });
  return { ...result, requested };
}

/** The value cell (dd) that follows a fact label (dt). */
function fact(drawer: HTMLElement, label: string): HTMLElement {
  const term = within(drawer).getByText(label, { selector: "dt" });
  return term.nextElementSibling as HTMLElement;
}

describe("audit event drawer", () => {
  it("renders the envelope, every change variant and both snapshots", async () => {
    const event = makeEvent({
      before: { name: "old", list: ["x"], src: 1 },
      after: { name: "new", list: ["x", "y"], dst: 1 },
      changes: [
        { op: "replace", path: "/name", value: "new" },
        { op: "add", path: "/list/1", value: "y" },
        { op: "move", from: "/src", path: "/dst" },
        { op: "remove", path: "" },
        { op: "frobnicate", path: "/zzz" },
      ],
    });
    const { requested } = openAt("/audit?event=e-1", event);
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    expect(await within(drawer).findByText("card.updated")).toBeVisible();
    expect(requested).toEqual(["e-1"]);

    expect(fact(drawer, "Occurred")).toHaveTextContent(
      "(2026-11-15T09:00:00Z)",
    );
    expect(
      within(fact(drawer, "Occurred")).getByText(
        (_, el) => el?.tagName === "TIME",
      ),
    ).toHaveAttribute("datetime", "2026-11-15T09:00:00Z");
    expect(fact(drawer, "Received")).not.toHaveTextContent(
      "2026-11-15T09:00:01Z",
    );
    expect(fact(drawer, "Actor")).toHaveTextContent("Ada via jwtlocal:u1");
    const object = fact(drawer, "Object");
    expect(within(object).getByRole("link")).toHaveAttribute(
      "href",
      "/cards/card-1",
    );
    expect(object).toHaveTextContent("(version 2)");
    expect(fact(drawer, "Service")).toHaveTextContent("contracts");
    expect(fact(drawer, "Request id")).toHaveTextContent("req-123456789");
    expect(fact(drawer, "Event id")).toHaveTextContent("e-1");

    expect(
      within(drawer).getByRole("heading", { name: "Changes (5)" }),
    ).toBeVisible();
    const rows = within(within(drawer).getByRole("table")).getAllByRole("row");
    const cells = (index: number) =>
      within(rows[index] as HTMLElement)
        .getAllByRole("cell")
        .concat(within(rows[index] as HTMLElement).getAllByRole("rowheader"))
        .map((cell) => cell.textContent);
    expect(cells(1)).toEqual(["Changed", "old", "new", "/name"]);
    expect(cells(2)).toEqual(["Added", "None", "y", "/list/1"]);
    expect(cells(3)).toEqual(["Moved", "None", "1", "/src → /dst"]);
    // The empty path is the whole document, shown as "/".
    expect(cells(4)).toEqual([
      "Removed",
      JSON.stringify(event.before),
      "None",
      "/",
    ]);
    // An unknown op keeps its own name; its target is absent.
    expect(cells(5)).toEqual(["frobnicate", "None", "None", "/zzz"]);

    expect(
      within(drawer).getByRole("region", { name: "Before snapshot" }),
    ).toHaveTextContent('"src": 1');
    expect(
      within(drawer).getByRole("region", { name: "After snapshot" }),
    ).toHaveTextContent('"dst": 1');
    expect(
      within(drawer).queryByRole("heading", { name: "Detail" }),
    ).toBeNull();
  });

  it("shows the fallbacks and the detail pairs of an access event", async () => {
    openAt(
      "/audit?event=e-2",
      makeEvent({
        event_id: "e-2",
        category: "access",
        action: "contract.document.read",
        aggregate_type: "system",
        aggregate_id: "s-1",
        aggregate_version: null,
        actor: { id: "local:u2" },
        request_id: null,
        received_at: "garbage",
        after: { a: 1 },
        detail: { format: "yaml", bytes: 42, empty: "", nothing: null },
      }),
    );
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    await within(drawer).findByText("contract.document.read");
    expect(within(drawer).getByText("Access")).toBeVisible();
    // No display name and no via: the id alone, not repeated as a secondary line.
    expect(fact(drawer, "Actor").textContent).toBe("local:u2");
    expect(fact(drawer, "Object").textContent).toBe("system s-1");
    expect(fact(drawer, "Request id")).toHaveTextContent("Not set");
    // An unparseable instant is shown as sent.
    expect(fact(drawer, "Received")).toHaveTextContent("garbage");
    expect(
      within(drawer).queryByRole("heading", { name: /Changes/ }),
    ).toBeNull();
    expect(
      within(drawer).getByRole("region", { name: "Before snapshot" }),
    ).toHaveTextContent("None (created)");

    expect(
      within(drawer).getByRole("heading", { name: "Detail" }),
    ).toBeVisible();
    expect(fact(drawer, "format")).toHaveTextContent("yaml");
    expect(fact(drawer, "bytes")).toHaveTextContent("42");
    expect(fact(drawer, "empty")).toHaveTextContent("—");
    expect(fact(drawer, "nothing")).toHaveTextContent("null");
  });

  it("shows a removed object and leaves out an empty detail", async () => {
    openAt(
      "/audit?event=e-3",
      makeEvent({
        event_id: "e-3",
        action: "card.archived",
        before: { a: 1 },
        detail: {},
      }),
    );
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    await within(drawer).findByText("card.archived");
    expect(
      within(drawer).getByRole("region", { name: "After snapshot" }),
    ).toHaveTextContent("None (removed)");
    expect(
      within(drawer).queryByRole("heading", { name: "Detail" }),
    ).toBeNull();
  });

  it("has no snapshots when neither side is set", async () => {
    openAt("/audit?event=e-4", makeEvent({ event_id: "e-4" }));
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    await within(drawer).findByText("card.updated");
    expect(
      within(drawer).queryByRole("heading", { name: "Before and after" }),
    ).toBeNull();
    expect(
      within(drawer).getByRole("button", { name: "Copy event JSON" }),
    ).toBeVisible();
  });

  it("shows a skeleton while the event loads", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    renderApp("/audit?event=e-5", {
      handlers: [
        emptyList,
        http.get("*/api/v1/audit/events/:id", async () => {
          await gate;
          return HttpResponse.json(makeEvent({ action: "late.event" }));
        }),
      ],
    });
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    expect(within(drawer).queryByText("late.event")).toBeNull();
    expect(
      within(drawer).queryByRole("heading", { name: "Not found" }),
    ).toBeNull();
    release();
    expect(await within(drawer).findByText("late.event")).toBeVisible();
  });

  it("renders Not found for an unknown event", async () => {
    openAt("/audit?event=missing", () =>
      HttpResponse.json(
        { type: "urn:dc:problem:not-found", title: "Not Found", status: 404 },
        {
          status: 404,
          headers: { "Content-Type": "application/problem+json" },
        },
      ),
    );
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    expect(
      await within(drawer).findByRole("heading", {
        level: 2,
        name: "Not found",
      }),
    ).toBeVisible();
    expect(
      within(drawer).getByText(
        "This item does not exist, or you do not have access to it.",
      ),
    ).toBeVisible();
  });

  it("shows any other problem and retries", async () => {
    const user = userEvent.setup();
    let calls = 0;
    openAt("/audit?event=e-6", () => {
      calls += 1;
      return calls === 1
        ? HttpResponse.json(
            {
              type: "urn:dc:problem:internal",
              title: "Audit broke",
              status: 500,
            },
            {
              status: 500,
              headers: { "Content-Type": "application/problem+json" },
            },
          )
        : HttpResponse.json(makeEvent({ action: "retried.event" }));
    });
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    expect(await within(drawer).findByText("Audit broke")).toBeVisible();
    await user.click(
      within(drawer).getByRole("button", { name: "Retry loading" }),
    );
    expect(await within(drawer).findByText("retried.event")).toBeVisible();
    expect(calls).toBe(2);
  });

  it("closes back to the applied filters", async () => {
    const user = userEvent.setup();
    const { router } = openAt(
      "/audit?service=contracts&event=e-1",
      makeEvent(),
    );
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    await within(drawer).findByText("card.updated");
    await user.click(
      within(drawer).getByRole("button", { name: "Close Audit event" }),
    );
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ service: "contracts" });
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Audit event" })).toBeNull();
    });
  });
});

describe("restore from a contract.deleted event", () => {
  const deleted = makeEvent({
    event_id: "e-del",
    aggregate_type: "contract",
    aggregate_id: "c-9",
    action: "contract.deleted",
    before: {
      id: "c-9",
      folder_id: "team-1",
      odcs_id: "orders",
      deleted_at: null,
    },
    after: {
      id: "c-9",
      folder_id: "team-1",
      odcs_id: "orders",
      deleted_at: "2026-11-15T09:00:00Z",
    },
  });

  function restoreAt(
    permissions: string[],
    restore: () => Response = () => HttpResponse.json({ id: "c-9" }),
  ) {
    const calls: string[] = [];
    renderApp("/audit?event=e-del", {
      permissions,
      handlers: [
        emptyList,
        http.get("*/api/v1/audit/events/:id", () => HttpResponse.json(deleted)),
        http.post("*/api/v1/contracts/contracts/:id/restore", ({ params }) => {
          calls.push(String(params.id));
          return restore();
        }),
      ],
    });
    return calls;
  }

  it("restores the contract and links to it", async () => {
    const user = userEvent.setup();
    const calls = restoreAt(["audit:read", "contract:restore"]);
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    await user.click(
      await within(drawer).findByRole("button", { name: "Restore contract" }),
    );
    expect(
      await within(drawer).findByText("orders was restored"),
    ).toBeVisible();
    expect(
      within(drawer).getByRole("link", { name: "Open orders" }),
    ).toHaveAttribute("href", "/contracts/c-9");
    expect(calls).toEqual(["c-9"]);
  });

  it("shows the API problem when the restore is refused", async () => {
    const user = userEvent.setup();
    restoreAt(["contract:restore"], () =>
      HttpResponse.json(
        {
          type: "urn:dc:problem:contract-referenced",
          title: "Contract referenced",
          status: 409,
          detail: "A published version links to a deleted contract.",
        },
        {
          status: 409,
          headers: { "Content-Type": "application/problem+json" },
        },
      ),
    );
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    await user.click(
      await within(drawer).findByRole("button", { name: "Restore contract" }),
    );
    expect(
      await within(drawer).findByText(
        "A published version links to a deleted contract.",
      ),
    ).toBeVisible();
    expect(
      within(drawer).getByRole("button", { name: "Restore contract" }),
    ).toBeVisible();
  });

  it("offers no restore without contract:restore", async () => {
    restoreAt(["audit:read"]);
    const drawer = await screen.findByRole("dialog", { name: "Audit event" });
    await within(drawer).findByText("contract.deleted");
    // Capabilities are loaded before the decision; give them a tick.
    await waitFor(() => {
      expect(
        within(drawer).queryByRole("button", { name: "Restore contract" }),
      ).toBeNull();
    });
    expect(within(drawer).queryByText("Deleted contract")).toBeNull();
  });
});
