// "Show contracts in tree" on /folders, through the real router: with the box ticked every
// expanded folder lists its own contracts (one list call per folder, include_subtree=false) as
// links; unticked the screen makes no tree call and a leaf folder has no chevron.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { makeContract } from "@/test/contracts-fixtures";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, ROOT_ID, rootFolder } from "@/test/renderApp";

afterEach(() => {
  tokenStore.clear();
});

const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const payments = makeFolder({
  id: "payments-id",
  parent_id: "acme-id",
  kind: "project",
  slug: "payments",
  display_name: "Payments",
  path: "acme/payments",
});
const ledger = makeFolder({
  id: "ledger-id",
  parent_id: "payments-id",
  kind: "team",
  slug: "ledger",
  display_name: "Ledger",
  path: "acme/payments/ledger",
});
const folders = [rootFolder, acme, payments, ledger];

type Call = { folderId: string | null; subtree: string | null };

/** A contract list answer that records its calls and serves rows per folder for the tree query. */
function contractList(byFolder: Record<string, unknown[]>) {
  const calls: Call[] = [];
  const handler = http.get("*/api/v1/contracts/contracts", ({ request }) => {
    const params = new URL(request.url).searchParams;
    const call = {
      folderId: params.get("folder_id"),
      subtree: params.get("include_subtree"),
    };
    calls.push(call);
    const items =
      call.subtree === "false" ? (byFolder[call.folderId ?? ""] ?? []) : [];
    return HttpResponse.json({ items, next_cursor: null });
  });
  return {
    handler,
    treeCalls: () => calls.filter((call) => call.subtree === "false"),
  };
}

describe("Show contracts in tree", () => {
  it("lists a folder's own contracts under its subfolders, each a link to the contract", async () => {
    const list = contractList({
      [ROOT_ID]: [
        makeContract({
          id: "c-root",
          folder_id: ROOT_ID,
          name: "Platform catalogue",
        }),
      ],
    });
    renderApp("/folders?contracts=true", { folders, handlers: [list.handler] });

    const link = await screen.findByRole("link", {
      name: "Platform catalogue",
    });
    expect(link).toHaveAttribute("href", "/contracts/c-root");
    const rows = screen.getAllByRole("row").slice(1);
    const names = rows.map((row) => row.getAttribute("aria-level"));
    // ROOT (level 1), then its organization (2), then its own contract one level below it (2).
    expect(rows[0]).toHaveAttribute("aria-level", "1");
    expect(rows[1]).toHaveAttribute("aria-level", "2");
    expect(within(rows[2] as HTMLElement).getByRole("link")).toBe(link);
    expect(names).toEqual(["1", "2", "2"]);
    expect(rows[2]).toHaveAttribute("aria-posinset", "1");
    expect(rows[2]).toHaveAttribute("aria-setsize", "1");
    expect(list.treeCalls()).toEqual([{ folderId: ROOT_ID, subtree: "false" }]);
  });

  it("expands a team that has no subfolders and shows its contracts one level deeper", async () => {
    const user = userEvent.setup();
    const list = contractList({
      "ledger-id": [
        makeContract({ id: "c-orders", name: "Orders" }),
        makeContract({ id: "c-refunds", name: "Refunds" }),
      ],
    });
    renderApp("/folders?contracts=true", { folders, handlers: [list.handler] });

    await user.click(await screen.findByRole("row", { name: "Acme" }));
    await user.click(await screen.findByRole("row", { name: "Payments" }));
    const team = await screen.findByRole("row", { name: "Ledger" });
    expect(team).toHaveAttribute("aria-expanded", "false");
    await user.click(team);

    const orders = await screen.findByRole("link", { name: "Orders" });
    expect(orders).toHaveAttribute("href", "/contracts/c-orders");
    expect(screen.getByRole("link", { name: "Refunds" })).toHaveAttribute(
      "href",
      "/contracts/c-refunds",
    );
    const teamLevel = Number(team.getAttribute("aria-level"));
    const row = orders.closest('[role="row"]');
    expect(row).toHaveAttribute("aria-level", String(teamLevel + 1));
    expect(row).toHaveAttribute("aria-posinset", "1");
    expect(row).toHaveAttribute("aria-setsize", "2");
    // One list call per expanded folder: ROOT, Acme, Payments and Ledger.
    await waitFor(() => {
      expect(
        list
          .treeCalls()
          .map((call) => call.folderId)
          .sort(),
      ).toEqual([ROOT_ID, "acme-id", "ledger-id", "payments-id"].sort());
    });
  });

  it("shows the odcs id for a contract with an empty name", async () => {
    const list = contractList({
      [ROOT_ID]: [
        makeContract({
          id: "c-unnamed",
          folder_id: ROOT_ID,
          name: "",
          odcs_id: "urn:acme:unnamed",
        }),
      ],
    });
    renderApp("/folders?contracts=true", { folders, handlers: [list.handler] });

    const link = await screen.findByRole("link", { name: "urn:acme:unnamed" });
    expect(link).toHaveAttribute("href", "/contracts/c-unnamed");
  });

  it("makes no tree request and gives a leaf folder no chevron when the box is unticked", async () => {
    const user = userEvent.setup();
    const list = contractList({
      "ledger-id": [makeContract({ id: "c-orders", name: "Orders" })],
    });
    renderApp("/folders", { folders, handlers: [list.handler] });

    await user.click(await screen.findByRole("row", { name: "Acme" }));
    await user.click(await screen.findByRole("row", { name: "Payments" }));
    const team = await screen.findByRole("row", { name: "Ledger" });
    expect(team).not.toHaveAttribute("aria-expanded");
    await user.click(team);

    expect(screen.queryByRole("link", { name: "Orders" })).toBeNull();
    expect(
      screen.getByRole("checkbox", { name: "Show contracts in tree" }),
    ).not.toBeChecked();
    expect(list.treeCalls()).toEqual([]);
  });
});

type Answer = {
  items?: unknown[];
  next_cursor?: string | null;
  status?: number;
};

/**
 * A scripted tree list: `respond(folderId, nth)` answers the nth tree call for a folder (an answer
 * with `status` fails it). Every call is recorded with its query, and any per-contract path
 * (/contracts/contracts/{id}...) is recorded as a violation of TREE-06 and answered 500.
 */
function scripted(
  respond: (folderId: string, nth: number) => Answer | Promise<Answer>,
) {
  const calls: { folderId: string; sort: string | null }[] = [];
  const perContract: string[] = [];
  const list = http.get("*/api/v1/contracts/contracts", async ({ request }) => {
    const params = new URL(request.url).searchParams;
    if (params.get("include_subtree") !== "false")
      return HttpResponse.json({ items: [], next_cursor: null });
    const folderId = params.get("folder_id") ?? "";
    calls.push({ folderId, sort: params.get("sort") });
    const nth = calls.filter((call) => call.folderId === folderId).length;
    const answer = await respond(folderId, nth);
    if (answer.status !== undefined)
      return HttpResponse.json({ title: "failed" }, { status: answer.status });
    return HttpResponse.json({
      items: answer.items ?? [],
      next_cursor: answer.next_cursor ?? null,
    });
  });
  const guard = http.all("*/api/v1/contracts/contracts/*", ({ request }) => {
    perContract.push(new URL(request.url).pathname);
    return HttpResponse.json({ title: "unexpected" }, { status: 500 });
  });
  return {
    handlers: [list, guard],
    calls,
    perContract,
    callsFor: (folderId: string) =>
      calls.filter((call) => call.folderId === folderId).length,
  };
}

describe("tree request count (TREE-06, D-03)", () => {
  it("makes one call per expanded folder, none on collapse, re-expand or untick, and no per-contract call", async () => {
    const user = userEvent.setup();
    const tree = scripted((folderId) => ({
      items: [makeContract({ id: `c-${folderId}`, name: `In ${folderId}` })],
    }));
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    await screen.findByRole("link", { name: "In " + ROOT_ID });
    await user.click(await screen.findByRole("row", { name: "Acme" }));
    await user.click(await screen.findByRole("row", { name: "Payments" }));
    await screen.findByRole("link", { name: "In payments-id" });
    // ROOT, Acme and Payments are open: exactly three calls, each by name with no subtree.
    expect(tree.calls).toHaveLength(3);
    expect(tree.calls.every((call) => call.sort === "name")).toBe(true);

    // Collapse Acme (hiding Payments) and open it again inside the stale time: no new call.
    await user.click(screen.getByRole("row", { name: "Acme" }));
    expect(screen.queryByRole("row", { name: "Payments" })).toBeNull();
    await user.click(screen.getByRole("row", { name: "Acme" }));
    await screen.findByRole("link", { name: "In payments-id" });
    expect(tree.calls).toHaveLength(3);

    // Unticking the box makes no call either.
    await user.click(
      screen.getByRole("checkbox", { name: "Show contracts in tree" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "In payments-id" })).toBeNull();
    });
    expect(tree.calls).toHaveLength(3);
    // Nothing of a contract's document, versions, drafts or validation was ever read.
    expect(tree.perContract).toEqual([]);
  });

  it("asks at most once for a folder that is opened, closed and opened again at once", async () => {
    const user = userEvent.setup();
    const tree = scripted(() => ({ items: [makeContract({ id: "c-1" })] }));
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    const acmeRow = await screen.findByRole("row", { name: "Acme" });
    await user.click(acmeRow);
    await user.click(acmeRow);
    await user.click(acmeRow);
    await waitFor(() => {
      expect(tree.callsFor("acme-id")).toBe(1);
    });
    expect(tree.callsFor("acme-id")).toBe(1);
  });
});

describe("archived folders in the tree (TREE-05, D-04)", () => {
  const old = makeFolder({
    id: "old-id",
    parent_id: "payments-id",
    kind: "team",
    slug: "old",
    display_name: "Old team",
    path: "acme/payments/old",
    archived: true,
  });
  const withArchived = [...folders, old];

  it("does not render an archived folder or call for its contracts without Show archived", async () => {
    const user = userEvent.setup();
    const tree = scripted(() => ({ items: [] }));
    renderApp("/folders?contracts=true", {
      folders: withArchived,
      handlers: tree.handlers,
    });

    await user.click(await screen.findByRole("row", { name: "Acme" }));
    await user.click(await screen.findByRole("row", { name: "Payments" }));
    await screen.findByRole("row", { name: "Ledger" });
    expect(screen.queryByRole("row", { name: /Old team/ })).toBeNull();
    await waitFor(() => {
      expect(tree.callsFor("payments-id")).toBe(1);
    });
    expect(tree.calls.map((call) => call.folderId)).not.toContain("old-id");
  });

  it("lists the contracts of an archived folder like any other once Show archived is ticked", async () => {
    const user = userEvent.setup();
    const tree = scripted((folderId) =>
      folderId === "old-id"
        ? { items: [makeContract({ id: "c-old", name: "Legacy orders" })] }
        : { items: [] },
    );
    renderApp("/folders?contracts=true&archived=true", {
      folders: withArchived,
      handlers: tree.handlers,
    });

    await user.click(await screen.findByRole("row", { name: "Acme" }));
    await user.click(await screen.findByRole("row", { name: "Payments" }));
    const archivedRow = await screen.findByRole("row", {
      name: "Old team, archived",
    });
    expect(tree.callsFor("old-id")).toBe(0);
    await user.click(archivedRow);
    expect(
      await screen.findByRole("link", { name: "Legacy orders" }),
    ).toHaveAttribute("href", "/contracts/c-old");
    expect(tree.callsFor("old-id")).toBe(1);
  });
});

describe("per-folder states of the tree (D-13 to D-15)", () => {
  const rowOf = (text: string) =>
    screen.getByText(text).closest<HTMLElement>('[role="row"]');

  it("shows one muted, non-focusable No contracts row for an empty folder, with no number", async () => {
    const tree = scripted(() => ({ items: [] }));
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    const message = await screen.findByText("No contracts");
    const row = rowOf("No contracts");
    expect(row).not.toBeNull();
    expect(row?.getAttribute("tabindex")).not.toBe("0");
    expect(row?.getAttribute("tabindex")).not.toBe("-1");
    expect(message.textContent).not.toMatch(/\d/);
    expect(screen.getAllByText("No contracts")).toHaveLength(1);
  });

  it("announces Loading contracts while a folder's list is in flight and keeps other folders usable", async () => {
    const user = userEvent.setup();
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const tree = scripted(async (folderId) => {
      if (folderId === ROOT_ID) await gate;
      return {
        items: [makeContract({ id: `c-${folderId}`, name: `In ${folderId}` })],
      };
    });
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    expect(await screen.findByText("Loading contracts")).toBeInTheDocument();
    const loadingRow = rowOf("Loading contracts");
    expect(loadingRow?.getAttribute("tabindex")).toBeNull();
    // The rest of the tree stays interactive while ROOT's list is pending.
    await user.click(await screen.findByRole("row", { name: "Acme" }));
    expect(screen.getByRole("row", { name: "Acme" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    await screen.findByRole("link", { name: "In acme-id" });

    await act(async () => {
      release();
      await gate;
    });
    await screen.findByRole("link", { name: "In " + ROOT_ID });
    expect(screen.queryByText("Loading contracts")).toBeNull();
  });

  it("shows an inline error with Retry for the failed folder only; Retry refetches just that folder", async () => {
    const user = userEvent.setup();
    const tree = scripted((folderId, nth) =>
      folderId === ROOT_ID && nth === 1
        ? { status: 500 }
        : {
            items: [
              makeContract({ id: `c-${folderId}`, name: `In ${folderId}` }),
            ],
          },
    );
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    expect(
      await screen.findByText("Could not load contracts."),
    ).toBeInTheDocument();
    await user.click(await screen.findByRole("row", { name: "Acme" }));
    // Acme's own list is fine while ROOT's failed.
    await screen.findByRole("link", { name: "In acme-id" });
    expect(screen.getAllByText("Could not load contracts.")).toHaveLength(1);
    expect(tree.callsFor(ROOT_ID)).toBe(1);
    expect(tree.callsFor("acme-id")).toBe(1);
    // The page-level folder list is untouched: the tree and no page alert are on screen.
    expect(screen.queryByRole("alert")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("link", { name: "In " + ROOT_ID }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Could not load contracts.")).toBeNull();
    expect(tree.callsFor(ROOT_ID)).toBe(2);
    expect(tree.callsFor("acme-id")).toBe(1);
    expect(tree.calls).toHaveLength(3);
  });

  it("ends a page that has more with a link to the matching list and no number", async () => {
    const tree = scripted(() => ({
      items: [makeContract({ id: "c-1", name: "Orders" })],
      next_cursor: "more-cursor-1",
    }));
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    const link = await screen.findByRole("link", {
      name: "More contracts — open folder",
    });
    expect(link).toHaveAttribute(
      "href",
      `/folders/${ROOT_ID}/contracts?subtree=false`,
    );
    expect(link.textContent).not.toMatch(/\d/);
    const rows = screen.getAllByRole("row");
    const last = rows.at(-1) as HTMLElement;
    expect(within(last).getByRole("link")).toBe(link);
    expect(last).toHaveAttribute("tabindex", "-1");
  });

  it("shows no overflow row for exactly one full page that is the last", async () => {
    const items = Array.from({ length: 100 }, (_, index) =>
      makeContract({
        id: `c-${String(index)}`,
        name: `Contract ${String(index).padStart(3, "0")}`,
      }),
    );
    const tree = scripted(() => ({ items, next_cursor: null }));
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    await screen.findByRole("link", { name: "Contract 000" });
    expect(screen.getAllByRole("link", { name: /^Contract / })).toHaveLength(
      100,
    );
    expect(screen.queryByText("More contracts — open folder")).toBeNull();
  });

  it("renders rows in the order the server sent them, not localeCompare order", async () => {
    const tree = scripted(() => ({
      items: [
        makeContract({ id: "c-z", name: "zeta" }),
        makeContract({ id: "c-a", name: "Alpha" }),
        makeContract({ id: "c-b", name: "beta" }),
      ],
    }));
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    await screen.findByRole("link", { name: "zeta" });
    const names = screen
      .getAllByRole("link", { name: /^(zeta|Alpha|beta)$/ })
      .map((link) => link.textContent);
    expect(names).toEqual(["zeta", "Alpha", "beta"]);
  });
});

describe("narrow tree: the lifecycle badge moves into the name cell", () => {
  it("carries the badge column's content in the name cell of a contract row, and not of a folder row", async () => {
    const tree = scripted(() => ({
      items: [makeContract({ id: "c-1", name: "Orders" })],
    }));
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    const link = await screen.findByRole("link", { name: "Orders" });
    const row = link.closest<HTMLElement>('[role="row"]');
    const nameCell = within(row as HTMLElement).getAllByRole("gridcell")[0];
    const inline = (nameCell as HTMLElement).querySelector(
      "[data-narrow-inline]",
    );
    expect(inline).not.toBeNull();
    expect(inline).toHaveTextContent("Draft");
    // The Kind column keeps its own copy for the wide tree.
    const kindCell = within(row as HTMLElement).getAllByRole("gridcell")[1];
    expect(kindCell).toHaveTextContent("Draft");
    // A folder row never carries the inline copy.
    const folderRow = screen.getByRole("row", { name: "Acme" });
    expect(folderRow.querySelector("[data-narrow-inline]")).toBeNull();
  });
});
