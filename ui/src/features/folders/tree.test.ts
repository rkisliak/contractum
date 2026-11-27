import { describe, expect, it } from "vitest";
import { makeFolder, rootFolder } from "@/test/renderApp";
import { suggestSlug, isValidSlug } from "./slug";
import type { ContractSummary } from "@/api/types";
import type { FolderContractsState } from "./api";
import {
  ancestorIds,
  buildFolderTree,
  folderName,
  rowLabel,
  visibleNodes,
  visibleRows,
} from "./tree";

const acme = makeFolder({ id: "acme", display_name: "Acme" });
const payments = makeFolder({
  id: "payments",
  parent_id: "acme",
  kind: "project",
  display_name: "Payments",
});
const ledger = makeFolder({
  id: "ledger",
  parent_id: "payments",
  kind: "team",
  display_name: "Ledger",
});
const fraud = makeFolder({
  id: "fraud",
  parent_id: "payments",
  kind: "team",
  display_name: "Fraud",
});
const orphan = makeFolder({
  id: "orphan",
  parent_id: "hidden-project",
  kind: "team",
  display_name: "Orphan team",
});

describe("buildFolderTree", () => {
  it("puts ROOT on top, nests by parent and sorts siblings by name", () => {
    const top = buildFolderTree([rootFolder, ledger, acme, fraud, payments]);
    expect(top.map((node) => node.folder.id)).toEqual([rootFolder.id]);
    const roots = top[0]?.children ?? [];
    expect(roots.map((node) => node.folder.id)).toEqual(["acme"]);
    expect(roots[0]?.depth).toBe(1);
    const project = roots[0]?.children[0];
    expect(project?.folder.id).toBe("payments");
    expect(project?.depth).toBe(2);
    expect(project?.children.map((node) => node.folder.display_name)).toEqual([
      "Fraud",
      "Ledger",
    ]);
    expect(project?.children[0]?.depth).toBe(3);
  });

  it("keeps a folder whose parent the caller cannot read at the top level", () => {
    const roots = buildFolderTree([acme, orphan]);
    expect(roots.map((node) => node.folder.id)).toEqual(["acme", "orphan"]);
  });

  it("lists visible nodes in display order, honouring expansion", () => {
    const roots = buildFolderTree([acme, payments, ledger, fraud]);
    expect(visibleNodes(roots, new Set()).map((n) => n.folder.id)).toEqual([
      "acme",
    ]);
    expect(
      visibleNodes(roots, new Set(["acme", "payments"])).map(
        (n) => n.folder.id,
      ),
    ).toEqual(["acme", "payments", "fraud", "ledger"]);
  });
});

describe("ancestorIds", () => {
  it("returns the readable chain from the top, nearest last", () => {
    expect(ancestorIds([rootFolder, acme, payments, ledger], "ledger")).toEqual(
      [rootFolder.id, "acme", "payments"],
    );
    expect(ancestorIds([orphan], "orphan")).toEqual([]);
  });
});

describe("folderName", () => {
  it("calls ROOT Root", () => {
    expect(folderName(rootFolder)).toBe("Root");
    expect(folderName(acme)).toBe("Acme");
  });
});

describe("slugs", () => {
  it("suggests a lowercase hyphenated slug", () => {
    expect(suggestSlug("  Card Payments & Fraud ")).toBe("card-payments-fraud");
    expect(suggestSlug("Zürich Ops")).toBe("zurich-ops");
    expect(suggestSlug("---")).toBe("");
    expect(suggestSlug("a".repeat(80))).toHaveLength(63);
  });

  it("accepts only the server's slug pattern", () => {
    expect(isValidSlug("ledger-eu-1")).toBe(true);
    expect(isValidSlug("")).toBe(false);
    expect(isValidSlug("-ledger")).toBe(false);
    expect(isValidSlug("led--ger")).toBe(false);
    expect(isValidSlug("Ledger")).toBe(false);
    expect(isValidSlug("a".repeat(64))).toBe(false);
  });
});

function contract(id: string, name: string | null = id): ContractSummary {
  return { id, name, odcs_id: `urn:${id}` } as ContractSummary;
}

function ready(
  items: ContractSummary[],
  more = false,
): Record<string, FolderContractsState> {
  return { ledger: { status: "ready", items, more } };
}

describe("visibleRows", () => {
  const roots = buildFolderTree([acme, payments, ledger, fraud]);
  const open = new Set(["acme", "payments", "ledger"]);
  const kinds = (rows: ReturnType<typeof visibleRows>) =>
    rows.map((row) => row.kind);

  it("yields exactly the folder rows of visibleNodes when contracts are off", () => {
    for (const expanded of [new Set<string>(), new Set(["acme"]), open]) {
      expect(visibleRows(roots, expanded, null).map((row) => row.key)).toEqual(
        visibleNodes(roots, expanded).map((node) => `f:${node.folder.id}`),
      );
    }
  });

  it("lists an expanded team's contracts below it, one level deeper, positions counted among them", () => {
    const rows = visibleRows(
      roots,
      open,
      ready([contract("a"), contract("b")]),
    );
    expect(kinds(rows)).toEqual([
      "folder",
      "folder",
      "folder",
      "folder",
      "contract",
      "contract",
    ]);
    const [first, second] = rows.slice(-2);
    const team = rows[3];
    expect(first).toMatchObject({
      key: "c:a",
      folderId: "ledger",
      position: 1,
      setSize: 2,
      depth: (team?.depth ?? 0) + 1,
    });
    expect(second).toMatchObject({ key: "c:b", position: 2, setSize: 2 });
  });

  it("puts a folder's subfolders before its own contracts", () => {
    const rows = visibleRows(roots, new Set(["acme", "payments"]), {
      payments: { status: "ready", items: [contract("p")], more: false },
    });
    expect(rows.map((row) => row.key)).toEqual([
      "f:acme",
      "f:payments",
      "f:fraud",
      "f:ledger",
      "c:p",
    ]);
  });

  it("emits one empty row for a ready folder with no contracts", () => {
    expect(kinds(visibleRows(roots, open, ready([])))).toEqual([
      "folder",
      "folder",
      "folder",
      "folder",
      "empty",
    ]);
  });

  it("emits one loading row while loading and one error row on failure", () => {
    const loading = visibleRows(roots, open, { ledger: { status: "loading" } });
    expect(kinds(loading).slice(4)).toEqual(["loading"]);
    const failed = visibleRows(roots, open, { ledger: { status: "error" } });
    expect(kinds(failed).slice(4)).toEqual(["error"]);
  });

  it("ends a page that has more with the contracts then one overflow row", () => {
    const rows = visibleRows(roots, open, ready([contract("a")], true));
    expect(kinds(rows).slice(4)).toEqual(["contract", "overflow"]);
    expect(rows.at(-1)?.key).toBe("o:ledger");
  });

  it("keeps the order the server sent, whatever localeCompare would say", () => {
    const rows = visibleRows(
      roots,
      open,
      ready([
        contract("z", "Zed"),
        contract("a", "Alpha"),
        contract("m", "mid"),
      ]),
    );
    expect(rows.slice(4).map((row) => row.key)).toEqual(["c:z", "c:a", "c:m"]);
  });

  it("emits nothing of its own for a collapsed folder or one with no state yet", () => {
    const collapsed = visibleRows(
      roots,
      new Set(["acme", "payments"]),
      ready([contract("a")]),
    );
    expect(collapsed.map((row) => row.kind)).toEqual([
      "folder",
      "folder",
      "folder",
      "folder",
    ]);
    expect(kinds(visibleRows(roots, open, {})).slice(4)).toEqual([]);
  });

  it("labels a contract row by its name, or by its odcs id when the name is empty", () => {
    const rows = visibleRows(
      roots,
      open,
      ready([contract("a", "Orders"), contract("b", ""), contract("c", null)]),
    );
    expect(rows.slice(4).map(rowLabel)).toEqual(["Orders", "urn:b", "urn:c"]);
    expect(rowLabel(rows[0] as never)).toBe("Acme");
  });
});
