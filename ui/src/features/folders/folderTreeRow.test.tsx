// A contract row of the folder tree: display name, lifecycle badge(s) and version right after the
// name, its open action and its full accessible name, all from the list row alone (TREE-02, D-08,
// D-10).
import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ContractSummary } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { makeContract } from "@/test/contracts-fixtures";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, ROOT_ID, rootFolder } from "@/test/renderApp";
import { contractRowLabel } from "./versionSlot";

afterEach(() => {
  tokenStore.clear();
});

function head(
  state: string,
  semver: string | null,
  deprecated = false,
): Record<string, unknown> {
  return {
    version: { number: 2, state, deprecated },
    latest_version: semver ? { version_id: `v-${semver}`, semver } : null,
  };
}

const rows = [
  makeContract({
    id: "c-latest",
    folder_id: ROOT_ID,
    name: "Orders",
    ...head("published", "1.2.0"),
  }),
  makeContract({
    id: "c-draft",
    folder_id: ROOT_ID,
    name: "Payments",
    ...head("draft", null),
  }),
  makeContract({
    id: "c-deprecated",
    folder_id: ROOT_ID,
    name: "Ledger",
    ...head("published", "2.0.0", true),
  }),
  makeContract({
    id: "c-next-draft",
    folder_id: ROOT_ID,
    name: "Refunds",
    ...head("draft", "1.1.0"),
  }),
  makeContract({
    id: "c-nameless",
    folder_id: ROOT_ID,
    name: "",
    odcs_id: "urn:acme:nameless",
    ...head("draft", null),
  }),
];

// The page shows the tree only when there is a folder below ROOT.
const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});

function renderTree() {
  const handler = http.get("*/api/v1/contracts/contracts", ({ request }) => {
    const subtree = new URL(request.url).searchParams.get("include_subtree");
    return HttpResponse.json({
      items: subtree === "false" ? rows : [],
      next_cursor: null,
    });
  });
  renderApp("/folders?contracts=true", {
    folders: [rootFolder, acme],
    handlers: [handler],
  });
}

async function contractRow(id: string): Promise<HTMLElement> {
  const contract = rows.find((candidate) => candidate.id === id);
  const label = contractRowLabel(contract as unknown as ContractSummary);
  return screen.findByRole("row", { name: label });
}

// The lifecycle badge(s) and version sit in the name cell, right after the name, at every width.
function meta(row: HTMLElement): HTMLElement {
  const nameCell = within(row).getAllByRole("gridcell")[0] as HTMLElement;
  const group = nameCell.querySelector<HTMLElement>("[data-contract-meta]");
  expect(group).not.toBeNull();
  return group as HTMLElement;
}

describe("a contract row in the folder tree", () => {
  it("shows the Latest badge and its semver", async () => {
    renderTree();
    const row = await contractRow("c-latest");
    expect(within(meta(row)).getByText("Latest")).toBeInTheDocument();
    expect(within(meta(row)).getByText("1.2.0")).toBeInTheDocument();
    // The badge comes first, then the version: "Latest 1.2.0", like the contract header.
    expect(meta(row)).toHaveTextContent(/^Latest\s*1\.2\.0$/);
    expect(within(meta(row)).queryByText("Deprecated")).not.toBeInTheDocument();
  });

  it("shows Draft and Not published for a draft", async () => {
    renderTree();
    const row = await contractRow("c-draft");
    expect(within(meta(row)).getByText("Draft")).toBeInTheDocument();
    expect(within(meta(row)).getByText("Not published")).toBeInTheDocument();
    expect(within(meta(row)).queryByText("Older")).not.toBeInTheDocument();
  });

  it("shows Latest and Deprecated for a deprecated Latest", async () => {
    renderTree();
    const row = await contractRow("c-deprecated");
    expect(within(meta(row)).getByText("Latest")).toBeInTheDocument();
    expect(within(meta(row)).getByText("Deprecated")).toBeInTheDocument();
    expect(within(meta(row)).getByText("2.0.0")).toBeInTheDocument();
  });

  it("names the Latest after the Draft badge of a published contract's draft head", async () => {
    renderTree();
    const row = await contractRow("c-next-draft");
    expect(meta(row)).toHaveTextContent(/^Draft\s*Latest 1\.1\.0$/);
    expect(
      screen.getByRole("row", { name: "Refunds, Draft, Latest version 1.1.0" }),
    ).toBe(row);
  });

  it("shows the odcs_id for a contract with an empty name", async () => {
    renderTree();
    const row = await contractRow("c-nameless");
    expect(within(row).getByText("urn:acme:nameless")).toBeInTheDocument();
    expect(within(meta(row)).getByText("Draft")).toBeInTheDocument();
  });

  it("names each row with its display name, badges and version", async () => {
    renderTree();
    await contractRow("c-latest");
    expect(
      screen.getByRole("row", { name: "Orders, Latest, version 1.2.0" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("row", { name: "Payments, Draft, not published" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("row", {
        name: "Ledger, Latest, Deprecated, version 2.0.0",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("row", {
        name: "urn:acme:nameless, Draft, not published",
      }),
    ).toBeInTheDocument();
  });

  it("spans the name cell over Kind, Subfolders and Contracts: no count or dash on a contract row", async () => {
    renderTree();
    const row = await contractRow("c-latest");
    const cells = within(row).getAllByRole("gridcell");
    // The name cell (spanning name, kind, subfolders, contracts), then the actions cell.
    expect(cells).toHaveLength(2);
    expect(cells[0]).toHaveAttribute("aria-colspan", "4");
    expect(cells[1]).toHaveAttribute("data-tree-action");
    expect(within(row).queryByText("—")).not.toBeInTheDocument();
  });

  it("gives a contract row the open action a folder row has, tabbable only on the tab stop", async () => {
    renderTree();
    const row = await contractRow("c-latest");
    const actions = within(row).getAllByRole("gridcell").at(-1) as HTMLElement;
    const open = within(actions).getByRole("link", { name: "Open Orders" });
    expect(open).toHaveAttribute("href", "/contracts/c-latest");
    // The Root row holds the tab stop, so the contract's action stays out of the Tab order.
    expect(row).toHaveAttribute("tabindex", "-1");
    expect(open).toHaveAttribute("tabindex", "-1");
    // The same slot layout as a folder row: an empty slot, then open.
    const folderOpen = within(
      screen.getByRole("row", { name: "Root" }),
    ).getByRole("link", { name: "Open Root" });
    expect(folderOpen.closest("[data-tree-action]")?.childElementCount).toBe(
      actions.childElementCount,
    );
  });

  it("names the open action of a nameless contract by its odcs_id", async () => {
    renderTree();
    const row = await contractRow("c-nameless");
    expect(
      within(row).getByRole("link", { name: "Open urn:acme:nameless" }),
    ).toHaveAttribute("href", "/contracts/c-nameless");
  });
});
