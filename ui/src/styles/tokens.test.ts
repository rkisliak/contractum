import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// UI-05 / D-UI-08-03, design system data-contracts-enterprise (2026-11-15): tokens.css is the one
// audited colour source. This test transcribes the registry (.claude/skills/typeui-design-system/
// colors.md, light and dark columns) and the measured WCAG 2.2 contrast of every shipped pairing in
// BOTH themes, so a drifting hex or a failing pairing breaks the build.

const css = readFileSync(resolve(import.meta.dirname, "tokens.css"), "utf-8");
const source = css.replace(/\/\*[\s\S]*?\*\//g, "");

/** Custom properties declared in one block of CSS text, first declaration wins. */
function declarations(block: string): Map<string, string> {
  const result = new Map<string, string>();
  for (const match of block.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    const [, name, value] = match;
    if (name !== undefined && value !== undefined && !result.has(name)) {
      result.set(name, value.trim().replace(/\s+/g, " "));
    }
  }
  return result;
}

function blockAfter(marker: string): string {
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`${marker} not found in tokens.css`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open + 1, i);
    }
  }
  throw new Error(`${marker} block is not closed`);
}

const light = declarations(blockAfter(":root"));
const darkOverrides = declarations(
  blockAfter("@media (prefers-color-scheme: dark)"),
);
const dark = new Map([...light, ...darkOverrides]);

// Registry, [light, dark], transcribed from colors.md. Keys are the registry names.
const registry: Record<string, [string, string]> = {
  white: ["#FFFFFF", "#FFFFFF"],
  heading: ["#111827", "#FFFFFF"],
  body: ["#4B5563", "#9CA3AF"],
  "body-subtle": ["#6B7280", "#9CA3AF"],
  "fg-brand": ["#1447E6", "#51A2FF"],
  "fg-brand-strong": ["#193CB8", "#BEDBFF"],
  "fg-danger": ["#BE123C", "#F43F5E"],
  "fg-danger-strong": ["#881337", "#F87171"],
  "fg-success-strong": ["#065F46", "#10B981"],
  "fg-warning": ["#7C2D12", "#FBBF24"],
  "fg-info": ["#312E81", "#93C5FD"],
  "neutral-primary": ["#FFFFFF", "#030712"],
  "neutral-primary-soft": ["#FFFFFF", "#101828"],
  "neutral-primary-medium": ["#FFFFFF", "#1E2939"],
  "neutral-secondary-soft": ["#F9FAFB", "#101828"],
  "neutral-secondary-medium": ["#F9FAFB", "#1E2939"],
  "neutral-tertiary-soft": ["#F3F4F6", "#101828"],
  "neutral-tertiary": ["#F3F4F6", "#1E2939"],
  "neutral-tertiary-medium": ["#F3F4F6", "#333E4F"],
  "neutral-quaternary": ["#E5E7EB", "#333E4F"],
  "brand-softer": ["#EEF6FF", "#162455"],
  "brand-soft": ["#DBEAFE", "#1C398E"],
  brand: ["#1447E6", "#155DFC"],
  "brand-strong": ["#193CB8", "#1447E6"],
  "success-soft": ["#ECFDF5", "#002C22"],
  "success-medium": ["#D0FAE5", "#004F3B"],
  success: ["#007A55", "#009966"],
  "danger-soft": ["#FEF0F2", "#4D0218"],
  "danger-medium": ["#FFE4E6", "#8B0836"],
  danger: ["#C70036", "#C70036"],
  "danger-strong": ["#A50036", "#A50036"],
  "warning-soft": ["#FFF7ED", "#7C2D12"],
  "warning-medium": ["#FFEDD5", "#7C2D12"],
  dark: ["#1F2937", "#1F2937"],
  disabled: ["#F3F4F6", "#1F2937"],
  "border-default": ["#E5E7EB", "#1F2937"],
  "border-default-medium": ["#E5E7EB", "#374151"],
  "border-default-strong": ["#E5E7EB", "#4B5563"],
  "border-brand": ["#1447E6", "#51A2FF"],
  "border-brand-subtle": ["#BEDBFF", "#1C398E"],
  "border-success-subtle": ["#A7F3D0", "#064E3B"],
  "border-danger-subtle": ["#FECDD3", "#881337"],
  "border-warning-subtle": ["#FED7AA", "#7C2D12"],
};

// App tokens: each maps onto registry values; the fundamentals exceptions are documented in tokens.css.
const appTokens: Record<string, [string, string]> = {
  // Icon rail, identical in both themes: dark, gray (light), dark-strong (dark).
  "rail-bg": ["#1F2937", "#1F2937"],
  "rail-fg": ["#D1D5DC", "#D1D5DC"],
  "rail-hover": ["#374151", "#374151"],
  // neutral-secondary-soft (light) / neutral-primary (dark)
  "surface-page": ["#F9FAFB", "#030712"],
  // body-subtle (light) / fg-disabled (dark): fundamentals SC 1.4.11
  "border-control": ["#6B7280", "#6B7280"],
  // brand (light) / border-brand (dark)
  "focus-ring": ["#1447E6", "#51A2FF"],
  // border-danger (light) / fg-danger-strong (dark): fundamentals SC 1.4.11
  "border-danger": ["#BE123C", "#F87171"],
};

// User-approved deviation (2026-11-18, tokens.css header): the light accent is one registry step
// deeper (brand-strong #193CB8, hover #1C398E). Registry values only; dark unchanged.
const userApproved: Record<string, [string, string]> = {
  "fg-brand": ["#193CB8", "#51A2FF"],
  "fg-brand-strong": ["#1C398E", "#BEDBFF"],
  brand: ["#193CB8", "#155DFC"],
  "brand-strong": ["#1C398E", "#1447E6"],
  "border-brand": ["#193CB8", "#51A2FF"],
  "focus-ring": ["#193CB8", "#51A2FF"],
};
const registryHexes = new Set(Object.values(registry).flat());
for (const hexes of Object.values(userApproved))
  for (const hex of hexes)
    if (!registryHexes.has(hex))
      throw new Error(`${hex} is not a registry colour`);

const expected = new Map<string, [string, string]>();
for (const [name, hexes] of Object.entries({
  ...registry,
  ...appTokens,
  ...userApproved,
})) {
  expected.set(`--color-${name}`, hexes);
}

function hexOf(theme: Map<string, string>, token: string): string {
  const value = theme.get(token);
  if (value === undefined)
    throw new Error(`${token} is not defined in tokens.css`);
  return value;
}

/** WCAG 2.2 relative luminance (sRGB linearised, 0.04045 threshold). */
function luminance(hex: string): number {
  const channels = [1, 3, 5].map((start) => {
    const c = Number.parseInt(hex.slice(start, start + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const [r = 0, g = 0, b = 0] = channels;
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(foreground: string, background: string): number {
  const a = luminance(foreground);
  const b = luminance(background);
  const [lighter, darker] = a >= b ? [a, b] : [b, a];
  return (lighter + 0.05) / (darker + 0.05);
}

describe("tokens.css against the enterprise registry", () => {
  it("defines every hex colour token with the registry value, light and dark", () => {
    const colours = [...light.entries()].filter(([, value]) =>
      /^#[0-9A-Fa-f]{6}$/.test(value),
    );
    expect(colours).toHaveLength(expected.size);
    for (const [name, value] of colours) {
      const hexes = expected.get(name);
      expect(hexes, `${name} has no registry entry`).toBeDefined();
      expect(value.toUpperCase(), `${name} light`).toBe(hexes?.[0]);
      expect(hexOf(dark, name).toUpperCase(), `${name} dark`).toBe(hexes?.[1]);
    }
  });

  it("redefines only colour tokens for the dark theme, and only known ones", () => {
    for (const name of darkOverrides.keys()) {
      expect(name.startsWith("--color-"), name).toBe(true);
      expect(light.has(name), `${name} only exists in dark`).toBe(true);
    }
  });

  it("keeps hex in the file's own case, uppercase", () => {
    for (const match of css.matchAll(/#([0-9A-Fa-f]{6})\b/g)) {
      expect(match[0]).toBe(match[0].toUpperCase());
    }
  });

  it("does not ship fg-disabled or a warning fill (white on warning is 2.8:1)", () => {
    for (const forbidden of [
      "--color-fg-disabled",
      "--color-warning",
      "--color-warning-strong",
    ]) {
      expect(light.has(forbidden), forbidden).toBe(false);
    }
    expect(contrast("#FFFFFF", "#F97316")).toBeLessThan(4.5);
  });

  it("holds the Inter 12/14/16/20/24/32 scale, the weights, radii and shadows from the skill", () => {
    expect(light.get("--font-size-xs")).toBe("0.75rem");
    expect(light.get("--font-size-sm")).toBe("0.875rem");
    expect(light.get("--font-size-md")).toBe("1rem");
    expect(light.get("--font-size-lg")).toBe("1.25rem");
    expect(light.get("--font-size-xl")).toBe("1.5rem");
    expect(light.get("--font-size-2xl")).toBe("2rem");
    expect(light.get("--font-weight-medium")).toBe("500");
    expect(light.get("--font-weight-semibold")).toBe("600");
    expect(light.get("--radius-control")).toBe("6px");
    expect(light.get("--radius-card")).toBe("8px");
    expect(light.get("--radius-overlay")).toBe("12px");
    expect(light.get("--radius-overlay-inner")).toBe(
      "calc(var(--radius-overlay) - var(--inset-overlay))",
    );
    expect(light.get("--shadow-xs")).toBe("0 1px 2px 0 rgb(0 0 0 / 0.05)");
    expect(light.get("--shadow-md")).toBe(
      "0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)",
    );
    expect(light.get("--shadow-xl")).toBe(
      "0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1)",
    );
    expect(light.get("--focus-ring-width")).toBe("2px");
    expect(light.get("--focus-ring-offset")).toBe("2px");
  });

  it("carries the dark scheme, the mobile input size and the reduced-motion block", () => {
    expect(css).toMatch(/@media \(prefers-color-scheme: dark\)/);
    expect(css).toMatch(
      /@media \(max-width: 767px\)\s*{\s*:root\s*{[^}]*--font-size-input: 1rem;/,
    );
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)\s*{\s*:root\s*{[^}]*--motion-hover: 0ms;[^}]*--motion-overlay: 0ms;/,
    );
  });
});

type Pair = {
  fg: string;
  bg: string;
  needs: number;
  light: number;
  dark: number;
  use: string;
};

// Measured contrast: text pairings need 4.5:1, boundaries, icons and focus need 3:1 (SC 1.4.11).
// The recorded ratios are the audit trail (see the build log contrast table).
const pairs: Pair[] = [
  {
    fg: "heading",
    bg: "neutral-primary-soft",
    needs: 4.5,
    light: 17.74,
    dark: 17.75,
    use: "headings, labels, input values on cards",
  },
  {
    fg: "heading",
    bg: "surface-page",
    needs: 4.5,
    light: 16.98,
    dark: 20.13,
    use: "headings on the page surface",
  },
  {
    fg: "body",
    bg: "neutral-primary-soft",
    needs: 4.5,
    light: 7.56,
    dark: 6.99,
    use: "body text on cards",
  },
  {
    fg: "body",
    bg: "surface-page",
    needs: 4.5,
    light: 7.23,
    dark: 7.93,
    use: "body text on the page surface",
  },
  {
    fg: "body",
    bg: "neutral-secondary-soft",
    needs: 4.5,
    light: 7.23,
    dark: 6.99,
    use: "table head, row hover",
  },
  {
    fg: "body",
    bg: "neutral-secondary-medium",
    needs: 4.5,
    light: 7.23,
    dark: 5.78,
    use: "placeholder, secondary fills, code blocks",
  },
  {
    fg: "body",
    bg: "neutral-primary-medium",
    needs: 4.5,
    light: 7.56,
    dark: 5.78,
    use: "menu items",
  },
  {
    fg: "body-subtle",
    bg: "neutral-primary-soft",
    needs: 4.5,
    light: 4.83,
    dark: 6.99,
    use: "meta labels, stat labels, sidebar section labels",
  },
  {
    fg: "body-subtle",
    bg: "surface-page",
    needs: 4.5,
    light: 4.63,
    dark: 7.93,
    use: "meta text on the page surface",
  },
  {
    fg: "heading",
    bg: "neutral-secondary-medium",
    needs: 4.5,
    light: 16.98,
    dark: 14.67,
    use: "input values, hovered cards",
  },
  {
    fg: "heading",
    bg: "neutral-tertiary-medium",
    needs: 4.5,
    light: 16.12,
    dark: 10.81,
    use: "hovered nav, menu and ghost buttons",
  },
  {
    fg: "heading",
    bg: "neutral-primary-medium",
    needs: 4.5,
    light: 17.74,
    dark: 14.67,
    use: "menu header",
  },
  {
    fg: "heading",
    bg: "brand-softer",
    needs: 4.5,
    light: 16.27,
    dark: 14.81,
    use: "selected list result",
  },
  {
    fg: "heading",
    bg: "danger-soft",
    needs: 4.5,
    light: 16.02,
    dark: 15.7,
    use: "invalid field value",
  },
  {
    fg: "fg-brand",
    bg: "neutral-primary-soft",
    needs: 4.5,
    light: 8.82,
    dark: 6.73,
    use: "links, active tab",
  },
  {
    fg: "fg-brand",
    bg: "surface-page",
    needs: 4.5,
    light: 8.44,
    dark: 7.63,
    use: "links on the page surface",
  },
  {
    fg: "fg-brand-strong",
    bg: "neutral-primary-soft",
    needs: 4.5,
    light: 10.37,
    dark: 12.48,
    use: "link hover",
  },
  {
    fg: "fg-brand-strong",
    bg: "brand-softer",
    needs: 4.5,
    light: 9.51,
    dark: 10.42,
    use: "active nav, info alert, brand badge, outline-brand hover",
  },
  {
    fg: "white",
    bg: "brand",
    needs: 4.5,
    light: 8.82,
    dark: 5.25,
    use: "primary button, brand tile",
  },
  {
    fg: "white",
    bg: "brand-strong",
    needs: 4.5,
    light: 10.37,
    dark: 6.83,
    use: "primary hover",
  },
  {
    fg: "white",
    bg: "danger",
    needs: 4.5,
    light: 6.03,
    dark: 6.03,
    use: "danger button",
  },
  {
    fg: "white",
    bg: "danger-strong",
    needs: 4.5,
    light: 7.92,
    dark: 7.92,
    use: "danger hover",
  },
  {
    fg: "white",
    bg: "dark",
    needs: 4.5,
    light: 14.68,
    dark: 14.68,
    use: "tooltip",
  },
  {
    fg: "fg-danger",
    bg: "neutral-primary-soft",
    needs: 4.5,
    light: 6.29,
    dark: 4.83,
    use: "outline danger, failing rule",
  },
  {
    fg: "fg-danger-strong",
    bg: "neutral-primary-medium",
    needs: 4.5,
    light: 9.57,
    dark: 5.3,
    use: "danger menu item",
  },
  {
    fg: "fg-danger-strong",
    bg: "neutral-primary-soft",
    needs: 4.5,
    light: 9.57,
    dark: 6.42,
    use: "field error text",
  },
  {
    fg: "fg-danger-strong",
    bg: "danger-soft",
    needs: 4.5,
    light: 8.64,
    dark: 5.68,
    use: "danger alert, badge, menu highlight",
  },
  {
    fg: "fg-success-strong",
    bg: "neutral-primary-soft",
    needs: 4.5,
    light: 7.68,
    dark: 7.0,
    use: "passing rule text",
  },
  {
    fg: "fg-success-strong",
    bg: "success-soft",
    needs: 4.5,
    light: 7.29,
    dark: 5.98,
    use: "success alert, badge, toast",
  },
  {
    fg: "fg-warning",
    bg: "warning-soft",
    needs: 4.5,
    light: 8.83,
    dark: 5.61,
    use: "warning alert, badge, boot failure",
  },
  {
    fg: "body-subtle",
    bg: "disabled",
    needs: 3.0,
    light: 4.39,
    dark: 5.78,
    use: "disabled label (inactive, fundamentals floor 3)",
  },
  {
    fg: "border-control",
    bg: "neutral-primary-soft",
    needs: 3.0,
    light: 4.83,
    dark: 3.67,
    use: "input, checkbox, outline-button boundary on cards",
  },
  {
    fg: "border-control",
    bg: "neutral-secondary-medium",
    needs: 3.0,
    light: 4.63,
    dark: 3.03,
    use: "input boundary against its own fill",
  },
  {
    fg: "border-control",
    bg: "surface-page",
    needs: 3.0,
    light: 4.63,
    dark: 4.16,
    use: "control boundary on the page surface",
  },
  {
    fg: "border-brand",
    bg: "neutral-secondary-medium",
    needs: 3.0,
    light: 8.44,
    dark: 5.56,
    use: "input focus border and ring",
  },
  {
    fg: "border-brand",
    bg: "neutral-primary-soft",
    needs: 3.0,
    light: 8.82,
    dark: 6.73,
    use: "tab and nav indicator",
  },
  {
    fg: "border-danger",
    bg: "neutral-primary-soft",
    needs: 3.0,
    light: 6.29,
    dark: 6.42,
    use: "invalid field border",
  },
  {
    fg: "border-danger",
    bg: "danger-soft",
    needs: 3.0,
    light: 5.67,
    dark: 5.68,
    use: "invalid field border against its fill",
  },
  {
    fg: "focus-ring",
    bg: "neutral-primary-soft",
    needs: 3.0,
    light: 8.82,
    dark: 6.73,
    use: "focus outline on cards",
  },
  {
    fg: "focus-ring",
    bg: "surface-page",
    needs: 3.0,
    light: 8.44,
    dark: 7.63,
    use: "focus outline on the page surface",
  },
  {
    fg: "focus-ring",
    bg: "neutral-primary-medium",
    needs: 3.0,
    light: 8.82,
    dark: 5.56,
    use: "focus outline in menus",
  },
  {
    fg: "brand",
    bg: "neutral-primary-soft",
    needs: 3.0,
    light: 8.82,
    dark: 3.38,
    use: "checked checkbox, brand tile",
  },
  {
    fg: "body",
    bg: "neutral-primary-soft",
    needs: 3.0,
    light: 7.56,
    dark: 6.99,
    use: "icons",
  },
  {
    fg: "body",
    bg: "neutral-tertiary",
    needs: 3.0,
    light: 6.87,
    dark: 5.78,
    use: "empty-state icon shape",
  },
  {
    fg: "fg-brand-strong",
    bg: "brand-softer",
    needs: 3.0,
    light: 9.51,
    dark: 10.42,
    use: "stat and quick-link icon shapes",
  },
  {
    fg: "body",
    bg: "danger-soft",
    needs: 4.5,
    light: 6.82,
    dark: 6.18,
    use: "placeholder in an invalid field",
  },
  {
    fg: "rail-fg",
    bg: "rail-bg",
    needs: 4.5,
    light: 9.97,
    dark: 9.97,
    use: "rail icons, active edge bar, rail focus ring",
  },
  {
    fg: "white",
    bg: "rail-hover",
    needs: 4.5,
    light: 10.31,
    dark: 10.31,
    use: "hovered rail icon",
  },
  {
    fg: "white",
    bg: "brand",
    needs: 3,
    light: 8.82,
    dark: 5.25,
    use: "icon on the active rail tile",
  },
  {
    fg: "fg-brand",
    bg: "brand-softer",
    needs: 4.5,
    light: 8.09,
    dark: 5.62,
    use: "active nav item and current tree row",
  },
];

describe("Measured contrast, WCAG 2.2, both themes", () => {
  it.each(pairs)(
    "$fg on $bg meets $needs:1 in light and dark ($use)",
    ({ fg, bg, needs, light: lightRatio, dark: darkRatio }) => {
      const f = `--color-${fg}`;
      const b = `--color-${bg}`;
      const measuredLight = contrast(hexOf(light, f), hexOf(light, b));
      const measuredDark = contrast(hexOf(dark, f), hexOf(dark, b));
      expect(measuredLight).toBeGreaterThanOrEqual(needs);
      expect(measuredDark).toBeGreaterThanOrEqual(needs);
      expect(measuredLight).toBeCloseTo(lightRatio, 1);
      expect(measuredDark).toBeCloseTo(darkRatio, 1);
    },
  );

  it("confirms the registry pairings rejected by the fundamentals still fail, so they stay unshipped", () => {
    const white = "#FFFFFF";
    // border-default(-medium) as a control boundary: 1.2:1.
    expect(
      contrast(hexOf(light, "--color-border-default-medium"), white),
    ).toBeLessThan(3);
    // buttons.md 4px brand-medium focus ring: 1.4:1.
    expect(contrast("#BEDBFF", white)).toBeLessThan(3);
    // fg-disabled for a disabled label: 2.3:1 on the disabled fill.
    expect(contrast("#9CA3AF", "#F3F4F6")).toBeLessThan(3);
    // registry dark border-danger on a dark card: 2.8:1.
    expect(contrast("#BE123C", "#101828")).toBeLessThan(3);
    // body-subtle on tertiary fills: 4.39:1, so meta text never sits on them.
    expect(contrast("#6B7280", "#F3F4F6")).toBeLessThan(4.5);
    // white on the dark success fill: 3.65:1, so there is no success button.
    expect(contrast(white, "#009966")).toBeLessThan(4.5);
  });
});
