import { resolve } from "node:path";
import stylelint from "stylelint";
import { describe, expect, it } from "vitest";

// D-UI-08-03 (enterprise tokens since 2026-11-15): the lint rules that keep components on the token sheet are exercised here,
// so a loosened .stylelintrc.json breaks a test and not just a future review.

const root = resolve(import.meta.dirname, "../..");

async function messages(code: string, file: string): Promise<string[]> {
  const result = await stylelint.lint({
    code,
    codeFilename: resolve(root, file),
    configFile: resolve(root, ".stylelintrc.json"),
  });
  return result.results.flatMap((r) => r.warnings.map((w) => w.rule));
}

describe(".stylelintrc.json token hygiene", () => {
  it("forbids raw hex colours in component CSS", async () => {
    expect(
      await messages(".a { color: #FFFFFF; }", "src/components/A/A.module.css"),
    ).toContain("color-no-hex");
  });

  it("allows hex only in tokens.css", async () => {
    expect(
      await messages(":root { --color-x: #FFFFFF; }", "src/styles/tokens.css"),
    ).not.toContain("color-no-hex");
  });

  it("forbids the glint parts, the disabled foreground and the retired Atlas tokens", async () => {
    for (const value of [
      "var(--color-1-400)",
      "var(--color-fg-disabled)",
      "var(--radius-xxl)",
      "var(--elevation-2)",
    ]) {
      const used = await messages(
        `.a { color: ${value}; }`,
        "src/components/A/A.module.css",
      );
      expect(used, value).toContain(
        "declaration-property-value-disallowed-list",
      );
    }
    expect(
      await messages(
        ":root { --color-1-400: red; }",
        "src/components/A/A.module.css",
      ),
    ).toContain("property-disallowed-list");
    expect(
      await messages(
        ":root { --color-1-400: rgb(255 255 255 / 0.25); }",
        "src/styles/tokens.css",
      ),
    ).toEqual([]);
  });

  it("allows the tokens that do exist", async () => {
    expect(
      await messages(
        ".a { color: var(--color-body-subtle); background: var(--color-success-soft); border-radius: var(--radius-card); }",
        "src/components/A/A.module.css",
      ),
    ).toEqual([]);
  });

  it("forbids px font sizes, but not rem or tokens", async () => {
    expect(
      await messages(
        ".a { font-size: 14px; }",
        "src/components/A/A.module.css",
      ),
    ).toContain("declaration-property-value-disallowed-list");
    expect(
      await messages(
        ".a { font: 600 14px/1.3 sans-serif; }",
        "src/components/A/A.module.css",
      ),
    ).toContain("declaration-property-value-disallowed-list");
    expect(
      await messages(
        ".a { font-size: var(--font-size-sm); } .b { font-size: 0.875rem; }",
        "src/components/A/A.module.css",
      ),
    ).toEqual([]);
  });

  it("forbids ad-hoc shadows and colour functions outside the sheet", async () => {
    const found = await messages(
      ".a { box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2); }",
      "src/components/A/A.module.css",
    );
    expect(found).toContain("declaration-property-value-allowed-list");
    expect(found).toContain("function-disallowed-list");
    expect(
      await messages(
        ".a { box-shadow: var(--shadow-xs), var(--shadow-lg); }",
        "src/components/A/A.module.css",
      ),
    ).toContain("declaration-property-value-allowed-list");
    expect(
      await messages(
        ".a { box-shadow: var(--shadow-md); } .b { box-shadow: var(--shadow-button); }",
        "src/components/A/A.module.css",
      ),
    ).toEqual([]);
  });
});
