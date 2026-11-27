// A contract row of the folder tree: display name, lifecycle badge(s), version slot and its full
// accessible name, all from the list row alone (TREE-02, D-08, D-10).
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

// A contract row carries its badges twice: in the Kind column and, hidden unless the tree is
// narrow, in the name cell (narrowInline). These tests read the Kind column's copy.
function kindCell(row: HTMLElement): HTMLElement {
  return within(row).getAllByRole("gridcell")[1] as HTMLElement;
}

describe("a contract row in the folder tree", () => {
  it("shows the Latest badge and its semver", async () => {
    renderTree();
    const row = await contractRow("c-latest");
    expect(within(kindCell(row)).getByText("Latest")).toBeInTheDocument();
    expect(within(row).getByText("1.2.0")).toBeInTheDocument();
    expect(
      within(kindCell(row)).queryByText("Deprecated"),
    ).not.toBeInTheDocument();
  });

  it("shows Draft and Not published for a draft", async () => {
    renderTree();
    const row = await contractRow("c-draft");
    expect(within(kindCell(row)).getByText("Draft")).toBeInTheDocument();
    expect(within(row).getByText("Not published")).toBeInTheDocument();
    expect(within(kindCell(row)).queryByText("Older")).not.toBeInTheDocument();
  });

  it("shows Latest and Deprecated for a deprecated Latest", async () => {
    renderTree();
    const row = await contractRow("c-deprecated");
    expect(within(kindCell(row)).getByText("Latest")).toBeInTheDocument();
    expect(within(kindCell(row)).getByText("Deprecated")).toBeInTheDocument();
    expect(within(row).getByText("2.0.0")).toBeInTheDocument();
  });

  it("shows the odcs_id for a contract with an empty name", async () => {
    renderTree();
    const row = await contractRow("c-nameless");
    expect(within(row).getByText("urn:acme:nameless")).toBeInTheDocument();
    expect(within(kindCell(row)).getByText("Draft")).toBeInTheDocument();
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

  it("leaves the Subfolders cell of a contract row empty, not a dash", async () => {
    renderTree();
    const row = await contractRow("c-latest");
    const cells = within(row).getAllByRole("gridcell");
    // name, kind, subfolders, contracts, actions-less: the subfolders cell is the third.
    expect(cells[2]).toHaveTextContent("");
    expect(within(row).queryByText("—")).not.toBeInTheDocument();
  });
});
