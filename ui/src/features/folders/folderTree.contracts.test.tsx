// "Show contracts in tree" on /folders, through the real router: with the box ticked every
// expanded folder lists its own contracts (one list call per folder, include_subtree=false) as
// links; unticked the screen makes no tree call and a leaf folder has no chevron.
import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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
    // The name link, then the row's open action, both to the contract.
    const [nameLink, openLink] = within(rows[2] as HTMLElement).getAllByRole(
      "link",
    );
    expect(nameLink).toBe(link);
    expect(openLink).toHaveAccessibleName("Open Platform catalogue");
    expect(openLink).toHaveAttribute("href", "/contracts/c-root");
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

  it("shows no row and no arrow for a folder whose contract list is empty", async () => {
    const tree = scripted(() => ({ items: [] }));
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    // Wait for the list call to settle, then the leaf folder has nothing under it.
    await waitFor(() => {
      expect(tree.calls.length).toBeGreaterThan(0);
    });
    await waitFor(() => {
      expect(screen.queryByText("Loading contracts")).toBeNull();
    });
    expect(screen.queryByText("No contracts")).toBeNull();
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

describe("contract row meta: the lifecycle badge and version follow the name", () => {
  it("puts the badge and version in the name cell of a contract row, and nothing in a folder row", async () => {
    const tree = scripted(() => ({
      items: [makeContract({ id: "c-1", name: "Orders" })],
    }));
    renderApp("/folders?contracts=true", { folders, handlers: tree.handlers });

    const link = await screen.findByRole("link", { name: "Orders" });
    const row = link.closest<HTMLElement>('[role="row"]');
    const cells = within(row as HTMLElement).getAllByRole("gridcell");
    const meta = (cells[0] as HTMLElement).querySelector(
      "[data-contract-meta]",
    );
    expect(meta).not.toBeNull();
    // A draft head of a contract with a Latest: the badge, then the Latest it does not replace.
    expect(meta).toHaveTextContent(/^Draft\s*Latest 1\.0\.0$/);
    // The name comes before the meta in the name cell.
    expect(
      link.compareDocumentPosition(meta as Node) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // The name cell spans the folder columns: the version is no longer in the Contracts column.
    expect(cells).toHaveLength(2);
    expect(cells[0]).toHaveAttribute("aria-colspan", "4");
    // A folder row never carries the meta group.
    const folderRow = screen.getByRole("row", { name: "Acme" });
    expect(folderRow.querySelector("[data-contract-meta]")).toBeNull();
  });
});

describe("tree keyboard model over every row kind (TREE-03, D-11)", () => {
  const beta = makeFolder({
    id: "beta-id",
    display_name: "Beta",
    path: "beta",
  });
  const orgs = [rootFolder, acme, beta];
  const rootItems = [
    makeContract({ id: "c-orders", name: "Orders" }),
    makeContract({ id: "c-feed", name: "Payments feed" }),
  ];
  const rootAnswer = (folderId: string): Answer => ({
    items: folderId === ROOT_ID ? rootItems : [],
  });

  const rowNamed = (name: string | RegExp) => screen.getByRole("row", { name });
  const orders = () => rowNamed(/^Orders,/);
  const feed = () => rowNamed(/^Payments feed,/);
  const tabStops = () =>
    screen
      .getAllByRole("row")
      .filter((row) => row.getAttribute("tabindex") === "0");
  const focus = (element: HTMLElement) => {
    act(() => {
      element.focus();
    });
  };

  it("moves Down and Up through folder and contract rows and skips an empty row", async () => {
    const user = userEvent.setup();
    const tree = scripted(rootAnswer);
    renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    await screen.findByRole("link", { name: "Orders" });
    // Acme is a leaf folder with no contracts: once its list comes back empty it shows nothing.
    await user.click(rowNamed("Acme"));
    await waitFor(() => {
      expect(rowNamed("Acme")).not.toHaveAttribute("aria-expanded");
    });
    expect(rowNamed("Acme")).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(rowNamed("Beta")).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(orders()).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(feed()).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(feed()).toHaveFocus();
    await user.keyboard("{ArrowUp}{ArrowUp}");
    expect(rowNamed("Beta")).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(rowNamed("Acme")).toHaveFocus();
  });

  it("gives a contract row no aria-expanded and keeps exactly one tab stop", async () => {
    const tree = scripted(rootAnswer);
    renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    await screen.findByRole("link", { name: "Orders" });
    expect(orders()).not.toHaveAttribute("aria-expanded");
    expect(tabStops()).toEqual([rowNamed("Root")]);
    focus(orders());
    expect(tabStops()).toEqual([orders()]);
    focus(feed());
    expect(tabStops()).toEqual([feed()]);
  });

  it("tabs from the focused contract row into its open action, and keys there belong to the link", async () => {
    const user = userEvent.setup();
    const tree = scripted(rootAnswer);
    renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    await screen.findByRole("link", { name: "Orders" });
    const openOrders = () =>
      within(orders()).getByRole("link", { name: "Open Orders" });
    const openFeed = () =>
      within(feed()).getByRole("link", { name: "Open Payments feed" });
    expect(openOrders()).toHaveAttribute("tabindex", "-1");
    focus(orders());
    // Only the tab-stop row's action is in the Tab order, as on folder rows.
    expect(openOrders()).toHaveAttribute("tabindex", "0");
    expect(openFeed()).toHaveAttribute("tabindex", "-1");
    await user.tab();
    expect(openOrders()).toHaveFocus();
    // Arrow keys on the action do not move the tree's focus.
    await user.keyboard("{ArrowDown}");
    expect(openOrders()).toHaveFocus();
    await user.tab({ shift: true });
    expect(orders()).toHaveFocus();
  });

  it("does nothing on Right over a contract row, and Left goes to its folder, then collapses it", async () => {
    const user = userEvent.setup();
    const tree = scripted(rootAnswer);
    renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    await screen.findByRole("link", { name: "Orders" });
    const before = screen.getAllByRole("row").length;
    focus(orders());
    await user.keyboard("{ArrowRight}");
    expect(orders()).toHaveFocus();
    expect(screen.getAllByRole("row")).toHaveLength(before);
    await user.keyboard("{ArrowLeft}");
    expect(rowNamed("Root")).toHaveFocus();
    expect(rowNamed("Root")).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{ArrowLeft}");
    expect(rowNamed("Root")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("row", { name: /^Orders,/ })).toBeNull();
    // Right opens it again and, on an open folder, enters it at its first child row.
    await user.keyboard("{ArrowRight}");
    expect(rowNamed("Root")).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{ArrowRight}");
    expect(rowNamed("Acme")).toHaveFocus();
  });

  it("jumps with Home and End over focusable rows and matches contract names by type-ahead", async () => {
    const user = userEvent.setup();
    const tree = scripted(rootAnswer);
    renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    await screen.findByRole("link", { name: "Orders" });
    focus(rowNamed("Root"));
    await user.keyboard("{End}");
    expect(feed()).toHaveFocus();
    await user.keyboard("{Home}");
    expect(rowNamed("Root")).toHaveFocus();
    await user.keyboard("o");
    expect(orders()).toHaveFocus();
    expect(tabStops()).toEqual([orders()]);
    await user.keyboard("p");
    expect(feed()).toHaveFocus();
    await user.keyboard("b");
    expect(rowNamed("Beta")).toHaveFocus();
    await user.keyboard("z");
    expect(rowNamed("Beta")).toHaveFocus();
    expect(tabStops()).toHaveLength(1);
  });

  it("leaves Ctrl, Meta and Alt shortcuts to the browser instead of treating them as type-ahead", async () => {
    const tree = scripted(rootAnswer);
    renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    await screen.findByRole("link", { name: "Orders" });
    focus(rowNamed("Root"));
    for (const modifier of ["ctrlKey", "metaKey", "altKey"] as const) {
      // fireEvent returns false when the handler called preventDefault.
      const notPrevented = fireEvent.keyDown(rowNamed("Root"), {
        key: "o",
        [modifier]: true,
      });
      expect(notPrevented).toBe(true);
      expect(rowNamed("Root")).toHaveFocus();
    }
    // A plain letter is still type-ahead and is handled.
    const plain = fireEvent.keyDown(rowNamed("Root"), { key: "o" });
    expect(plain).toBe(false);
    expect(orders()).toHaveFocus();
  });

  it("leaves Alt+Arrow (browser back and forward) and other modified navigation keys to the browser", async () => {
    const tree = scripted(rootAnswer);
    renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    await screen.findByRole("link", { name: "Orders" });
    focus(rowNamed("Root"));
    const expanded = rowNamed("Root").getAttribute("aria-expanded");
    const keys = [
      "ArrowLeft",
      "ArrowRight",
      "ArrowDown",
      "ArrowUp",
      "Home",
      "End",
      "Enter",
      " ",
    ];
    for (const modifier of ["altKey", "ctrlKey", "metaKey"] as const) {
      for (const key of keys) {
        // fireEvent returns false when the handler called preventDefault.
        const notPrevented = fireEvent.keyDown(rowNamed("Root"), {
          key,
          [modifier]: true,
        });
        expect(notPrevented, `${modifier}+${key}`).toBe(true);
        expect(rowNamed("Root")).toHaveFocus();
        expect(rowNamed("Root").getAttribute("aria-expanded")).toBe(expanded);
      }
    }
    // The unmodified keys still drive the model.
    expect(fireEvent.keyDown(rowNamed("Root"), { key: "ArrowDown" })).toBe(
      false,
    );
    expect(rowNamed("Root")).not.toHaveFocus();
  });

  it("falls back to the folder row as the tab stop when the focused contract row disappears", async () => {
    const user = userEvent.setup();
    const tree = scripted(rootAnswer);
    renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    await screen.findByRole("link", { name: "Orders" });
    focus(orders());
    const toggle = rowNamed("Root").querySelector("[data-tree-toggle]");
    await user.click(toggle as Element);
    expect(rowNamed("Root")).toHaveAttribute("aria-expanded", "false");
    expect(tabStops()).toEqual([rowNamed("Root")]);
    expect(rowNamed("Root")).toHaveFocus();
  });

  it.each([
    ["Enter", "{Enter}"],
    ["Space", " "],
  ])("opens the contract on %s", async (_name, key) => {
    const user = userEvent.setup();
    const tree = scripted(rootAnswer);
    const { router } = renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    await screen.findByRole("link", { name: "Orders" });
    focus(orders());
    await user.keyboard(key);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/c-orders");
    });
  });

  it("opens the contract on a click anywhere on the row, and the name stays a real link", async () => {
    const user = userEvent.setup();
    const tree = scripted(rootAnswer);
    const { router } = renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    const link = await screen.findByRole("link", { name: "Orders" });
    expect(link).toHaveAttribute("href", "/contracts/c-orders");
    const cells = within(orders()).getAllByRole("gridcell");
    await user.click(cells[1] as HTMLElement);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/c-orders");
    });
  });

  it("enters the matching list on Enter over the overflow row and does nothing on Right", async () => {
    const user = userEvent.setup();
    const tree = scripted(() => ({
      items: [makeContract({ id: "c-1", name: "Orders" })],
      next_cursor: "more-cursor-1",
    }));
    const { router } = renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    const overflow = (
      await screen.findByRole("link", { name: "More contracts — open folder" })
    ).closest<HTMLElement>('[role="row"]') as HTMLElement;
    focus(overflow);
    expect(tabStops()).toEqual([overflow]);
    await user.keyboard("{ArrowRight}");
    expect(overflow).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(
        `/folders/${ROOT_ID}/contracts`,
      );
    });
    expect(router.state.location.search).toEqual({ subtree: false });
  });

  it.each([
    ["Enter", "{Enter}"],
    ["Space", " "],
  ])(
    "retries only that folder on %s over the error row",
    async (_name, key) => {
      const user = userEvent.setup();
      const tree = scripted((folderId, nth) =>
        folderId === ROOT_ID && nth === 1
          ? { status: 500 }
          : { items: [makeContract({ id: "c-1", name: "Orders" })] },
      );
      renderApp("/folders?contracts=true", {
        folders: orgs,
        handlers: tree.handlers,
      });

      const errorRow = (
        await screen.findByText("Could not load contracts.")
      ).closest<HTMLElement>('[role="row"]') as HTMLElement;
      await user.keyboard("{End}");
      focus(errorRow);
      expect(tabStops()).toEqual([errorRow]);
      await user.keyboard("{ArrowRight}");
      expect(errorRow).toHaveFocus();
      expect(tree.callsFor(ROOT_ID)).toBe(1);
      await user.keyboard(key);
      await screen.findByRole("link", { name: "Orders" });
      expect(tree.callsFor(ROOT_ID)).toBe(2);
      expect(tree.calls).toHaveLength(2);
      // Focus is not lost to the page when the error row is replaced: it lands on the folder row.
      expect(rowNamed("Root")).toHaveFocus();
    },
  );

  it("makes the Retry button tabbable only while its row holds the tab stop", async () => {
    const tree = scripted(() => ({ status: 500 }));
    renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    const retry = await screen.findByRole("button", { name: "Retry loading" });
    expect(retry).toHaveAttribute("tabindex", "-1");
    focus(retry.closest<HTMLElement>('[role="row"]') as HTMLElement);
    expect(retry).toHaveAttribute("tabindex", "0");
  });

  it("does not move focus when a finished load inserts rows", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const tree = scripted(async (folderId) => {
      if (folderId === ROOT_ID) await gate;
      return rootAnswer(folderId);
    });
    renderApp("/folders?contracts=true", {
      folders: orgs,
      handlers: tree.handlers,
    });

    await screen.findByText("Loading contracts");
    focus(rowNamed("Beta"));
    await act(async () => {
      release();
      await gate;
    });
    await screen.findByRole("link", { name: "Orders" });
    expect(rowNamed("Beta")).toHaveFocus();
    expect(tabStops()).toEqual([rowNamed("Beta")]);
  });
});
