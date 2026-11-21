// The crawl shared by the layout audit (`layout.spec.ts`) and the volume audit (`volume.spec.ts`):
// settle a page, measure it (`checkLayout`), screenshot it, and walk the overlays, tabs and
// in-page states a page's buttons open. A pure move out of layout.spec.ts (quick 261120-6i0,
// D-07): a small context object carries the screenshot root, the `opened` and `crawled` sets and
// an optional per-state hook (the volume spec runs axe and the issue check there).
import type { Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { checkLayout } from "./layout-helpers";

export const WIDTHS = { desktop: 1440, laptop: 1024, phone: 390 } as const;
export type Width = keyof typeof WIDTHS;

/** What a measured state is, for the per-state hook. */
export type StateKind = "route" | "state";

export type CrawlContext = {
  /** Screenshots go to `test-results/<root>/<project>/<width>/<state>.png`. */
  root: string;
  /** Titles of every overlay opened (checked against the coverage registry). */
  opened: Set<string>;
  /** `width|path|button` keys already clicked (each opens once per page and width). */
  crawled: Set<string>;
  /**
   * Opens a row action (a button inside a table row) once per verb and page, not once per row:
   * with hundreds of rows "Revoke vol-pat-001", "Revoke vol-pat-002", ... are one overlay.
   */
  oncePerRowAction?: boolean;
  /** Runs after the layout check of every state (axe and the issue check in the volume spec). */
  onState?: (
    page: Page,
    width: Width,
    label: string,
    kind: StateKind,
  ) => Promise<void>;
};

export function newContext(
  root: string,
  onState?: CrawlContext["onState"],
  oncePerRowAction = false,
): CrawlContext {
  return {
    root,
    oncePerRowAction,
    opened: new Set<string>(),
    crawled: new Set<string>(),
    ...(onState ? { onState } : {}),
  };
}

/**
 * Page buttons the crawl never clicks: they write, navigate away for good, or are noise.
 * "Expand editor" only opens the full-screen editor dialog, so the crawl opens it; "Expand
 * navigation" stays denied.
 */
export const DENY =
  /^(save|validate|check draft|replace draft|reload|keep my edits|download|load more|retry|sign out$|apply filters|clear filters|me$|last |past |unarchive|restore|go to line|copy|show |hide |run dry-run|swap|dismiss|leave without saving|keep editing|expand(?! editor)|collapse|open navigation|close|previous|next|create contract|go back|test connection|export|reset to default|keep forever|delete after)/i;

export const OVERLAY = "[role=dialog], [role=alertdialog], [role=menu]";

export const slug = (s: string) =>
  s
    .replace(/^https?:\/\/[^/]+/, "")
    .replace(/[0-9a-f]{8}-[0-9a-f-]{27}/g, "id")
    .replace(/[^a-zA-Z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 90) || "home";

export async function settle(page: Page): Promise<void> {
  await page
    .waitForLoadState("networkidle", { timeout: 5_000 })
    .catch(() => undefined);
  await expect(page.locator("[aria-busy='true']"))
    .toHaveCount(0, { timeout: 8_000 })
    .catch(() => undefined);
  // Monaco and lazy chunks lay out one frame after load.
  await page.waitForTimeout(150);
}

export async function audit(
  ctx: CrawlContext,
  page: Page,
  width: Width,
  label: string,
  fullPage = true,
  kind: StateKind = "state",
): Promise<void> {
  await settle(page);
  await checkLayout(page, `${label} @${String(WIDTHS[width])}`);
  if (ctx.onState) await ctx.onState(page, width, label, kind);
  await page.screenshot({
    path: `test-results/${ctx.root}/${test.info().project.name}/${width}/${slug(label)}.png`,
    fullPage,
  });
}

export async function nameOfOverlay(overlay: Locator): Promise<string> {
  return overlay.evaluate((el) => {
    const id = el.getAttribute("aria-labelledby");
    const byId = id
      ? id
          .split(" ")
          .map((x) => document.getElementById(x)?.textContent ?? "")
          .join(" ")
      : "";
    return (el.getAttribute("aria-label") ?? byId).replace(/\s+/g, " ").trim();
  });
}

export async function dismiss(page: Page): Promise<void> {
  for (let i = 0; i < 3; i++) {
    const open = page.locator(OVERLAY).filter({ visible: true });
    if ((await open.count()) === 0) return;
    await page.keyboard.press("Escape");
    await page.waitForTimeout(100);
  }
  const close = page.getByRole("button", { name: "Close dialog" });
  if (await close.isVisible().catch(() => false)) await close.click();
}

/** Visits a URL and audits it as a route; fails the h1 check when the page has none. */
export async function visitAndAudit(
  ctx: CrawlContext,
  page: Page,
  width: Width,
  url: string,
): Promise<void> {
  await page.goto(url);
  await expect(
    page.getByRole("heading", { level: 1 }).first(),
    `${url}: h1`,
  ).toBeVisible({ timeout: 15_000 });
  await audit(ctx, page, width, url, true, "route");
}

/** Opens every overlay, tab and in-page form reachable from page buttons on the current URL. */
export async function crawl(
  ctx: CrawlContext,
  page: Page,
  width: Width,
  url: string,
): Promise<void> {
  const main = page.locator("main");
  // 1. In-page tabs (Radix Tabs; route tabs are links and are visited as routes).
  // A URL that names its tab (`?tab=`) is one of several listed per tab; the tabs are clicked
  // only on a URL without one, then the page is reloaded so the buttons below are its own.
  const tabNames = url.includes("tab=")
    ? []
    : await main.getByRole("tab").allInnerTexts();
  for (const name of tabNames) {
    const tab = main.getByRole("tab", { name: name.trim(), exact: true });
    if (!(await tab.isVisible().catch(() => false))) continue;
    await tab.click();
    await audit(ctx, page, width, `${url} tab ${name.trim()}`);
  }
  if (tabNames.length > 0) {
    await page.goto(url);
    await settle(page);
  }

  // 2. Buttons that open something.
  const found = await main
    .locator("button:not([type=submit]):not([disabled])")
    .filter({ visible: true })
    .evaluateAll((els) =>
      els
        .filter(
          (el) =>
            !el.closest(
              "[role=dialog],[role=alertdialog],[role=treegrid],thead",
            ),
        )
        .map((el) => ({
          name: (el.getAttribute("aria-label") ?? el.textContent)
            .replace(/\s+/g, " ")
            .trim(),
          inRow: el.closest("tbody tr") !== null,
        })),
    );
  const names = found.map((f) => f.name);
  const inRow = new Map(found.map((f) => [f.name, f.inRow]));
  const path = new URL(url, "https://x").pathname;
  for (const name of names) {
    if (!name || DENY.test(name)) continue;
    // Row actions repeat per row ("Revoke token-a", "Revoke token-b") and header actions repeat
    // on every tab URL of one page: each opens once per page and width.
    // "Sign out <device>" row buttons and the "Sign out everywhere else" header button share their
    // first two words, so those names are keyed on three words to open each overlay once.
    const words =
      ctx.oncePerRowAction && inRow.get(name) === true
        ? 1
        : /^sign out /i.test(name)
          ? 3
          : 2;
    const key = `${width}|${path}|${name.split(" ").slice(0, words).join(" ").toLowerCase()}`;
    if (ctx.crawled.has(key)) continue;
    ctx.crawled.add(key);
    const button = main.getByRole("button", { name, exact: true }).first();
    if (!(await button.isVisible().catch(() => false))) continue;
    if (await button.isDisabled().catch(() => true)) continue;
    const isMenu = (await button.getAttribute("aria-haspopup")) === "menu";
    await button.click();
    await page.waitForTimeout(250);
    const overlay = page.locator(OVERLAY).filter({ visible: true }).last();
    if (await overlay.isVisible().catch(() => false)) {
      const title = isMenu ? `menu ${name}` : await nameOfOverlay(overlay);
      ctx.opened.add(title);
      await audit(ctx, page, width, `${url} ${title}`, false);
      if (isMenu) {
        const items = await overlay
          .locator("[role=menuitem]:not([aria-disabled=true]):not(a)")
          .allInnerTexts();
        await dismiss(page);
        for (const item of items.map((t) => t.trim())) {
          if (!item || DENY.test(item)) continue;
          await button.click();
          await page.getByRole("menuitem", { name: item, exact: true }).click();
          await page.waitForTimeout(250);
          const dlg = page.locator(OVERLAY).filter({ visible: true }).last();
          if (await dlg.isVisible().catch(() => false)) {
            const t = await nameOfOverlay(dlg);
            ctx.opened.add(t);
            await audit(ctx, page, width, `${url} ${t}`, false);
          }
          await dismiss(page);
          if (
            new URL(page.url()).pathname !== new URL(url, page.url()).pathname
          ) {
            await page.goto(url);
            await settle(page);
          }
        }
      }
      await dismiss(page);
    } else if (
      new URL(page.url()).pathname + new URL(page.url()).search !==
      url
    ) {
      // A navigation: audited as a route elsewhere; come back.
      await page.goto(url);
      await settle(page);
      continue;
    } else {
      // An in-page state (a form or editor mode): audit it, then reset.
      await audit(ctx, page, width, `${url} after ${name}`);
      await page.goto(url);
      await settle(page);
    }
    await expect(page.locator(OVERLAY).filter({ visible: true })).toHaveCount(
      0,
    );
  }
}
