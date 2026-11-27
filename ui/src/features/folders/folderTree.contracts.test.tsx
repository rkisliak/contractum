// "Show contracts in tree" on /folders, through the real router: with the box ticked every
// expanded folder lists its own contracts (one list call per folder, include_subtree=false) as
// links; unticked the screen makes no tree call and a leaf folder has no chevron.
import { screen, waitFor, within } from "@testing-library/react";
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
