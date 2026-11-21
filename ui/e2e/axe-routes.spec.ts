// Axe on every route (UI-16, Phase 13 success criteria 4). The route list comes from the router's
// own route tree (`src/routeTree.gen.ts`, copied into the runner image): every leaf route must
// have URLs in `layout-coverage.ts` ROUTES (signed-in) or be one of the public routes checked
// here, so a new route without an axe visit fails this spec. Each URL is then loaded as the
// Global admin (public routes signed out) and must show its h1 with no serious or critical axe
// violation (WCAG 2.2 AA tag set, the same gate as the journeys).
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import {
  adminApi,
  findDemo,
  signIn,
  softAxe,
  type Demo,
} from "./journey-helpers";
import { PUBLIC_ROUTES, ROUTES } from "./layout-coverage";

test.use({ actionTimeout: 15_000, navigationTimeout: 30_000 });

/** Route ids of `FileRoutesById` in the generated route tree. */
function routeIds(): string[] {
  const source = readFileSync(
    join(process.cwd(), "src", "routeTree.gen.ts"),
    "utf8",
  );
  const block = /export interface FileRoutesById \{([\s\S]*?)\n\}/.exec(
    source,
  )?.[1];
  if (!block) throw new Error("FileRoutesById not found in routeTree.gen.ts");
  return [...block.matchAll(/^\s+'([^']+)': typeof/gm)]
    .map((m) => m[1] ?? "")
    .filter((id) => id.startsWith("/"));
}

/** Leaf routes: a layout route (`/_app/contracts/$contractId`) renders through its children. */
function leaves(ids: string[]): string[] {
  return ids.filter(
    (id) =>
      id !== "/_app" &&
      !ids.some((other) => other !== id && other.startsWith(`${id}/`)),
  );
}

/** `/_app/contracts/$contractId/` -> `contracts/$contractId/index` (the ROUTES key). */
function appKey(id: string): string {
  const rest = id.slice("/_app/".length);
  if (rest === "") return "index";
  return rest.endsWith("/") ? `${rest}index` : rest;
}

/** `/signin_/rotate` -> `signin_.rotate` (the PUBLIC_ROUTES key). */
function publicKey(id: string): string {
  return id.slice(1).replace(/\//g, ".");
}

const PUBLIC_VISITS: Record<string, { url: string; h1: string | RegExp }> = {
  signin: { url: "/signin", h1: /.+/ },
  // Without the one-time rotation token the route sends the visitor back to sign-in.
  "signin_.rotate": { url: "/signin/rotate", h1: /.+/ },
  "auth.callback": { url: "/auth/callback", h1: "Sign in with Microsoft" },
};

let demo: Demo;

test.beforeAll(async () => {
  const api = await adminApi();
  try {
    demo = await findDemo(api);
  } finally {
    await api.dispose();
  }
});

async function axeVisit(
  page: Page,
  url: string,
  h1: string | RegExp,
): Promise<void> {
  await page.goto(url);
  await expect
    .soft(
      page.getByRole("heading", { level: 1, name: h1 }).first(),
      `${url}: h1`,
    )
    .toBeVisible({ timeout: 20_000 });
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await expect(page.locator("[aria-busy='true']"))
    .toHaveCount(0, { timeout: 10_000 })
    .catch(() => undefined);
  await softAxe(page, url);
}

test("every route in the route tree has an axe visit", () => {
  const ids = leaves(routeIds());
  expect(ids.length).toBeGreaterThan(40);
  const app = ids.filter((id) => id.startsWith("/_app/"));
  const pub = ids.filter((id) => !id.startsWith("/_app"));
  const missingApp = app
    .map(appKey)
    .filter(
      (key) =>
        !(key in ROUTES) ||
        (ROUTES[key]?.(demo) ?? []).filter(Boolean).length === 0,
    );
  expect(missingApp, "signed-in routes without an axe visit").toEqual([]);
  const missingPublic = pub
    .map(publicKey)
    .filter((key) => !(key in PUBLIC_VISITS) || !(key in PUBLIC_ROUTES));
  expect(missingPublic, "public routes without an axe visit").toEqual([]);
});

test("public routes have no serious axe violations", async ({ page }) => {
  test.setTimeout(180_000);
  for (const { url, h1 } of Object.values(PUBLIC_VISITS))
    await axeVisit(page, url, h1);
});

test("signed-in routes have no serious axe violations", async ({ page }) => {
  test.setTimeout(1_800_000);
  await page.setViewportSize({ width: 1440, height: 960 });
  await signIn(page);
  const urls = new Set<string>();
  for (const id of leaves(routeIds()).filter((i) => i.startsWith("/_app/")))
    for (const url of ROUTES[appKey(id)]?.(demo) ?? []) if (url) urls.add(url);
  expect(urls.size).toBeGreaterThan(40);
  for (const url of urls) await axeVisit(page, url, /.+/);
});
