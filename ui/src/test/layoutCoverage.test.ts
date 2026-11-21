// Guards the layout audit's coverage (e2e/layout.spec.ts): every leaf route file under
// src/routes has URLs in e2e/layout-coverage.ts ROUTES (or a reason in PUBLIC_ROUTES), and every
// source file that renders a Dialog, ConfirmDialog, SecretRevealDialog, Drawer or Menu has title
// patterns in OVERLAYS, which the e2e run must then open. A new page or overlay without layout
// coverage fails here.
import { describe, expect, it } from "vitest";
import {
  OVERLAYS,
  POPUPS,
  PUBLIC_ROUTES,
  ROUTES,
  VOLUME_ROUTES,
} from "../../e2e/layout-coverage";

const routeFiles = Object.keys(import.meta.glob("/src/routes/**/*.tsx"))
  .filter((f) => !/\.test\.tsx$/.test(f))
  .map((f) => f.replace(/^\/src\/routes\//, "").replace(/\.tsx$/, ""));

const sources = import.meta.glob<string>(
  ["/src/features/**/*.tsx", "/src/components/**/*.tsx"],
  { query: "?raw", import: "default", eager: true },
);

describe("layout audit coverage", () => {
  it("lists every signed-in leaf route", () => {
    const app = routeFiles
      .filter((f) => f.startsWith("_app/"))
      .map((f) => f.slice("_app/".length));
    // A layout route (`$contractId.tsx` beside a `$contractId/` folder) renders through its leaves.
    const leaves = app.filter((f) => !app.some((g) => g.startsWith(`${f}/`)));
    expect(leaves.length).toBeGreaterThan(35);
    expect(leaves.filter((f) => !(f in ROUTES)).sort()).toEqual([]);
    expect(Object.keys(ROUTES).filter((r) => !leaves.includes(r))).toEqual([]);
  });

  it("lists every public route or why it is left out", () => {
    const pub = routeFiles.filter(
      (f) => !f.startsWith("_app") && f !== "__root",
    );
    expect(pub.filter((f) => !(f in PUBLIC_ROUTES)).sort()).toEqual([]);
  });

  it("lists every component that renders an overlay", () => {
    const overlayFiles = Object.entries(sources)
      .filter(
        ([path, text]) =>
          !path.endsWith(".test.tsx") &&
          /<(Dialog|ConfirmDialog|SecretRevealDialog|Drawer|Menu)[\s>]/.test(
            text,
          ),
      )
      .map(([path]) => path.replace(/^\/src\//, ""))
      // The shared primitives render each other; their users are what the audit opens.
      .filter((p) => !p.startsWith("components/"));
    expect(overlayFiles.filter((f) => !(f in OVERLAYS)).sort()).toEqual([]);
    for (const [file, patterns] of Object.entries(OVERLAYS)) {
      expect(overlayFiles, file).toContain(file);
      expect(patterns.length, file).toBeGreaterThan(0);
    }
  });

  it("gives the volume audit the same routes as the layout audit", () => {
    expect(Object.keys(VOLUME_ROUTES).sort()).toEqual(
      Object.keys(ROUTES).sort(),
    );
  });

  it("lists every file that renders a searchable popup", () => {
    // The wrappers themselves (PrincipalPicker, EntraDirectorySearch) and the Combobox primitive
    // render the popup for their users, which are what the audit types into.
    const popupFiles = Object.entries(sources)
      .filter(
        ([path, text]) =>
          !path.endsWith(".test.tsx") &&
          /<(Combobox|PrincipalPicker|EntraDirectorySearch)[\s>]/.test(text),
      )
      .map(([path]) => path.replace(/^\/src\//, ""))
      .filter(
        (p) =>
          !p.startsWith("components/") &&
          p !== "features/admin/PrincipalPicker.tsx" &&
          p !== "features/admin/EntraDirectorySearch.tsx",
      );
    expect(popupFiles.filter((f) => !(f in POPUPS)).sort()).toEqual([]);
    for (const [file, patterns] of Object.entries(POPUPS)) {
      expect(Object.keys(sources), file).toContain(`/src/${file}`);
      expect(patterns.length, file).toBeGreaterThan(0);
    }
  });
});
