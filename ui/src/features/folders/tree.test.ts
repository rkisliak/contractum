import { describe, expect, it } from "vitest";
import { makeFolder, rootFolder } from "@/test/renderApp";
import { suggestSlug, isValidSlug } from "./slug";
import { ancestorIds, buildFolderTree, folderName, visibleNodes } from "./tree";

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
