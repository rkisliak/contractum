import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Regression guards for the high-volume rendering audit (quick 261120-6i0): the fixes that are
// pure CSS cannot be seen in jsdom, so the declarations that make them work are pinned here (the
// measured proof is the volume Playwright spec, rules R5, R6, R16 and R17).
const src = resolve(import.meta.dirname, "..");
const css = (file: string) => readFileSync(resolve(src, file), "utf-8");

describe("long and right-to-left user text", () => {
  it("isolates every text container from bidi reordering (V-06, R17)", () => {
    const global = css("styles/global.css");
    expect(global).toMatch(/unicode-bidi:\s*isolate/);
    for (const tag of ["td", "li", "a", "span", "h1", "button"])
      expect(global).toMatch(new RegExp(`\\n\\s+${tag},?\\n`));
  });

  it("lets a table cell wrap an overlong word instead of widening the table (V-01, R6)", () => {
    const table = css("components/DataTable/DataTable.module.css");
    expect(table).toMatch(
      /\[data-longword\]\s*\{[^}]*max-inline-size:\s*28rem/,
    );
    expect(table).toMatch(
      /\[data-longword\] \*\s*\{[^}]*overflow-wrap:\s*anywhere/,
    );
  });

  it("wraps a table caption that names a long folder (V-01, R6)", () => {
    const table = css("components/DataTable/DataTable.module.css");
    expect(table).toMatch(/\.caption\s*\{[^}]*overflow-wrap:\s*anywhere/);
  });

  it("keeps a breadcrumb crumb on one line with an ellipsis (V-02, R16)", () => {
    const crumb = css("components/Breadcrumb/Breadcrumb.module.css");
    expect(crumb).toMatch(/text-overflow:\s*ellipsis/);
    expect(crumb).toMatch(/white-space:\s*nowrap/);
  });

  it("caps a badge and ends its label in an ellipsis (V-05, R16)", () => {
    const badge = css("components/Badge/Badge.module.css");
    expect(badge).toMatch(/max-inline-size:\s*min\(100%,\s*16rem\)/);
    expect(badge).toMatch(/\.label\s*\{[^}]*text-overflow:\s*ellipsis/);
  });

  it("keeps the page header from growing with its widest child (V-03, R5)", () => {
    const header = css("components/PageHeader/PageHeader.module.css");
    expect(header).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)/);
    expect(header).toMatch(/\.description\s*\{[^}]*overflow-wrap:\s*anywhere/);
  });

  it("wraps the access summary text (V-04, R5)", () => {
    const access = css("features/access/EffectiveAccess.module.css");
    expect(access).toMatch(/\.muted\s*\{[^}]*overflow-wrap:\s*anywhere/);
  });

  it("wraps a contact name in the card facts (V-07, R5)", () => {
    const cards = css("features/cards/Cards.module.css");
    expect(cards).toMatch(/\.facts dt\s*\{[^}]*overflow-wrap:\s*anywhere/);
    expect(cards).toMatch(/\.facts\s*\{[^}]*fit-content\(16rem\)/);
  });

  it("caps a button and ends a long label in an ellipsis (V-11, R15)", () => {
    const button = css("components/Button/Button.module.css");
    expect(button).toMatch(
      /\.text\s*\{[^}]*max-inline-size:\s*min\(22rem,\s*70vw\)/,
    );
    expect(button).toMatch(/\.text\s*\{[^}]*text-overflow:\s*ellipsis/);
  });

  it("gives a narrow tree's contract name the name cell and wraps its badges and version under it (R5)", () => {
    const tree = css("features/folders/FolderTree.module.css");
    const narrow = tree.slice(tree.indexOf("@container (max-width: 36rem)"));
    const wide = tree.slice(0, tree.indexOf("@container (max-width: 36rem)"));
    // The contract row keeps its actions cell (the open action) on a narrow tree: nothing hides it.
    expect(narrow).not.toMatch(/\.contractRow > \[role="gridcell"\]/);
    expect(narrow).toMatch(/\.contractRow \.row\s*\{[^}]*flex-wrap:\s*wrap/);
    expect(narrow).toMatch(/\.contractRow \.label\s*\{[^}]*flex:\s*1 1 0/);
    expect(narrow).toMatch(/\.contractMeta\s*\{[^}]*flex:\s*0 0 100%/);
    // The name cell spans the folder columns up to the actions column at every width.
    expect(wide).toMatch(/\.spanToActions\s*\{[^}]*grid-column:\s*1 \/ -2/);
    // On a wide tree the meta follows the name directly and never shrinks; the name ellipsises.
    expect(wide).toMatch(/\.contractRow \.label\s*\{[^}]*flex:\s*0 1 auto/);
    expect(wide).toMatch(/\.contractMeta\s*\{[^}]*flex-shrink:\s*0/);
    expect(wide).toMatch(/\.label\s*\{[^}]*text-overflow:\s*ellipsis/);
  });
});
