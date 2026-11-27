// Layout audit: every route in `layout-coverage.ts` ROUTES and every state reachable from it
// (each dialog, confirm, drawer and menu a page button opens, every in-page tab, the shell modes,
// empty, not-found and validation-error states) is measured by `checkLayout` (see
// layout-helpers.ts for the ten rules) and screenshotted into
// test-results/audit/<project>/<width>/<state>.png for visual review. Routes run at 1440, 1024
// and 390; the state crawl runs at 1440 and 390. Both the light (chromium) and dark
// (chromium-dark) projects run it. The last step asserts coverage: every listed route was
// visited and every listed overlay title was opened.
//
// The crawl clicks page buttons that only open something (dialogs, menus, forms); buttons that
// write directly (save, validate, download, restore...) and every submit are never clicked, and
// an opened overlay is always dismissed with Escape, so the audit leaves the demo data unchanged
// apart from one personal access token it creates (for the show-once dialog) and revokes.
import { expect, test } from "./fixtures";
import type { Page } from "@playwright/test";
import {
  audit as auditState,
  crawl as crawlPage,
  dismiss,
  nameOfOverlay,
  newContext,
  settle,
  visitAndAudit as visitPage,
  WIDTHS,
  type Width,
} from "./layout-crawl";
import { OVERLAYS, ROUTES } from "./layout-coverage";
import { adminApi, findDemo, signIn, type Demo } from "./journey-helpers";
import {
  ensurePersonas,
  LEDGER_EDITOR,
  LEDGER_SUBSCRIBER,
  signInAs,
} from "./personas";

let demo: Demo;
const ctx = newContext("audit");
const opened = ctx.opened;
const visited = new Set<string>();

// The shared crawl (layout-crawl.ts) bound to this audit's context.
const audit = (page: Page, width: Width, label: string, fullPage = true) =>
  auditState(ctx, page, width, label, fullPage);
const visitAndAudit = (page: Page, width: Width, url: string) =>
  visitPage(ctx, page, width, url);
const crawl = (page: Page, width: Width, url: string) =>
  crawlPage(ctx, page, width, url);

test.use({
  actionTimeout: 10_000,
  navigationTimeout: 30_000,
  reducedMotion: "reduce",
});

test.beforeAll(async () => {
  const api = await adminApi();
  try {
    demo = await findDemo(api);
    await ensurePersonas(api);
  } finally {
    await api.dispose();
  }
});

function allUrls(): { route: string; url: string }[] {
  const out: { route: string; url: string }[] = [];
  for (const [route, make] of Object.entries(ROUTES)) {
    const urls = make(demo);
    for (const url of urls) {
      expect
        .soft(url, `${route}: the demo data has no record for it`)
        .toBeTruthy();
      if (url) out.push({ route, url });
    }
  }
  return out;
}

async function explicitStates(page: Page, width: Width): Promise<void> {
  // /contracts/new: every source option, the upload picker and an invalid submit.
  const newUrl = `/contracts/new?folder=${demo.ledger.id}`;
  await page.goto(newUrl);
  await settle(page);
  const radios = page.locator("main input[type=radio]");
  const count = await radios.count();
  for (let i = 0; i < count; i++) {
    const radio = radios.nth(i);
    const label = await radio.evaluate((el) =>
      (el.closest("label")?.textContent ?? el.getAttribute("aria-label") ?? "")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 40),
    );
    await radio.check({ force: true });
    await audit(page, width, `${newUrl} source ${label}`);
  }
  await radios.first().check({ force: true });
  const nameField = page.getByLabel("Name", { exact: true });
  if (await nameField.isVisible().catch(() => false)) await nameField.fill("");
  const odcs = page.getByLabel("ODCS id");
  if (await odcs.isVisible().catch(() => false)) await odcs.fill("");
  await page.getByRole("button", { name: "Create contract" }).click();
  await audit(page, width, `${newUrl} invalid submit`);

  // Create folder dialog with an empty name submitted (field error).
  await page.goto(`/folders/${demo.payments.id}`);
  await settle(page);
  await page.getByRole("button", { name: "Create team" }).first().click();
  const createTeam = page.getByRole("dialog", { name: "Create team" });
  await createTeam.getByRole("button", { name: "Create team" }).click();
  await audit(page, width, "create team invalid submit", false);
  await dismiss(page);

  // Create user dialog submitted empty.
  await page.goto("/admin/users");
  await settle(page);
  await page.getByRole("button", { name: "New" }).first().click();
  await page.getByRole("menuitem", { name: "Create local user" }).click();
  const createUser = page.getByRole("dialog", { name: "Create local user" });
  await createUser.getByRole("button", { name: "Create local user" }).click();
  await audit(page, width, "create user invalid submit", false);
  await dismiss(page);

  // The show-once secret: a personal access token, created and revoked.
  await page.goto("/account/tokens");
  await settle(page);
  const tokenName = `layout-${Date.now().toString(36)}`;
  await page.getByRole("button", { name: "Create token" }).click();
  const createToken = page.getByRole("dialog", { name: "Create token" });
  await createToken.getByLabel("Name").fill(tokenName);
  await createToken.getByRole("button", { name: "Create token" }).click();
  const reveal = page.getByRole("dialog", { name: /^Your new / });
  await expect(reveal).toBeVisible();
  opened.add(await nameOfOverlay(reveal));
  await audit(page, width, "secret reveal dialog", false);
  await reveal.getByLabel(/^I have stored this /).check();
  await audit(page, width, "secret reveal dialog acknowledged", false);
  await reveal.getByRole("button", { name: /^Close after storing / }).click();
  await expect(reveal).toBeHidden();
  const row = page.getByRole("row", { name: new RegExp(tokenName) });
  // On a stack that also holds the volume dataset the admin has over a hundred tokens: the new
  // one may sit on a later page of the list.
  for (
    let i = 0;
    i < 8 &&
    !(await row
      .first()
      .isVisible()
      .catch(() => false));
    i++
  ) {
    const more = page.getByRole("button", { name: /^Load more/ }).first();
    if (!(await more.isVisible().catch(() => false))) break;
    await more.click();
    await settle(page);
  }
  await row.getByRole("button", { name: /^Revoke/ }).click();
  const revoke = page.getByRole("alertdialog");
  const confirmText = revoke.getByRole("textbox");
  if (await confirmText.count()) await confirmText.fill(tokenName);
  await audit(page, width, "revoke token confirm filled", false);
  await revoke.getByRole("button", { name: `Revoke ${tokenName}` }).click();
  await expect(revoke).toBeHidden();

  // The account menu in the shell.
  await page.goto("/");
  await settle(page);
  if (width === "phone") {
    await page.getByRole("button", { name: "Open navigation" }).click();
    const drawer = page.getByRole("dialog", { name: "Navigation" });
    await expect(drawer).toBeVisible();
    opened.add("Navigation");
    await audit(page, width, "navigation drawer", false);
    await dismiss(page);
  }
  // The rail's avatar at 1440, the top bar's at 390 (the other one is hidden by CSS).
  const account = page
    .getByRole("button", { name: /account menu/i })
    .filter({ visible: true })
    .first();
  await account.click();
  await expect(page.getByRole("menu")).toBeVisible();
  opened.add("menu account");
  await audit(page, width, "account menu", false);
  // "Versions" from the account menu (also the rail's info button at 1440).
  await page.getByRole("menuitem", { name: "Versions" }).click();
  const versions = page.getByRole("dialog", { name: "Versions" });
  await expect(versions).toBeVisible();
  await expect(
    versions.getByRole("rowheader", { name: "API docs" }),
  ).toBeVisible();
  await expect(versions.getByText("Loading…")).toHaveCount(0);
  opened.add("Versions");
  await audit(page, width, "versions dialog", false);
  await dismiss(page);

  // The audit event drawer.
  await page.goto("/audit");
  await settle(page);
  await page
    .getByRole("table")
    .locator("tbody tr")
    .first()
    .getByRole("button")
    .first()
    .click();
  const drawer = page.getByRole("dialog").last();
  await expect(drawer).toBeVisible();
  opened.add(await nameOfOverlay(drawer));
  await audit(page, width, "audit event drawer", false);
  await dismiss(page);

  // Effective access with the details open, one area row expanded and the technical permission
  // names shown (folder Access tab and home).
  for (const url of [`/folders/${demo.ledger.id}/access`, "/"]) {
    await page.goto(url);
    await settle(page);
    const details = page.getByText("See details", { exact: true });
    if (await details.count()) await details.first().click();
    await page
      .getByRole("table", { name: /^What you can do/ })
      .first()
      .getByRole("button")
      .first()
      .click();
    await page.getByText("Show technical permission names").first().click();
    await audit(page, width, `${url} access details`);
  }

  // The /folders navigator with every row expanded (kind badges, counts, row actions).
  await page.goto("/folders");
  await settle(page);
  const tree = page.getByRole("treegrid", { name: "All folders" });
  for (let i = 0; i < 4; i++) {
    const closed = tree.locator("[role=row][aria-expanded=false]");
    const n = await closed.count();
    for (let j = 0; j < n; j++)
      await closed.first().locator("[data-tree-toggle]").click();
  }
  // Each toggle click scrolls its row into view; with many folders (a warm stack) the phone
  // layout's sticky top bar then sits over scrolled rows, which R10 would report as an overlap.
  // Measure the expanded tree from the top of the page.
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.getElementById("main-content")?.scrollTo(0, 0);
  });
  await settle(page);
  await audit(page, width, "/folders expanded");

  // The same navigator with "Show contracts in tree" on and every folder expanded: contract rows
  // (name, lifecycle badge, version slot, the narrow-inline badge at 390) beside the folder rows.
  await page.goto("/folders?contracts=true");
  await settle(page);
  for (let i = 0; i < 4; i++) {
    const closed = tree.locator("[role=row][aria-expanded=false]");
    const n = await closed.count();
    for (let j = 0; j < n; j++)
      await closed.first().locator("[data-tree-toggle]").click();
  }
  await expect(page.locator("[aria-busy='true']")).toHaveCount(0);
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.getElementById("main-content")?.scrollTo(0, 0);
  });
  await settle(page);
  await audit(page, width, "/folders?contracts=true expanded");

  // Not found.
  await page.goto("/contracts/00000000-0000-4000-8000-000000000000");
  await audit(page, width, "contract not found");
  await page.goto("/no-such-page");
  await audit(page, width, "unknown url");
}

test.describe("layout audit", () => {
  test("every route, signed out and signed in, at 1440, 1024 and 390", async ({
    page,
  }) => {
    test.setTimeout(30 * 60_000);
    for (const width of Object.keys(WIDTHS) as Width[]) {
      await page.setViewportSize({ width: WIDTHS[width], height: 900 });
      await page.context().clearCookies();
      await page.goto("/signin");
      await audit(page, width, "/signin");
      await signIn(page);
      for (const { route, url } of allUrls()) {
        await visitAndAudit(page, width, url);
        visited.add(route);
      }
    }
    for (const route of Object.keys(ROUTES))
      expect.soft(visited.has(route), `route ${route} was audited`).toBe(true);
  });

  test("every dialog, menu, drawer, tab and form state at 1440 and 390", async ({
    page,
  }) => {
    test.setTimeout(60 * 60_000);
    for (const width of ["desktop", "phone"] as Width[]) {
      await page.setViewportSize({ width: WIDTHS[width], height: 900 });
      await page.context().clearCookies();
      await signIn(page);
      for (const { url } of allUrls()) {
        await page.goto(url);
        await settle(page);
        await crawl(page, width, url);
      }
      await explicitStates(page, width);
    }
    console.log(`overlays opened:\n${[...opened].sort().join("\n")}`);
    for (const [file, patterns] of Object.entries(OVERLAYS)) {
      for (const pattern of patterns) {
        expect
          .soft(
            [...opened].some((t) => pattern.test(t)),
            `${file}: an overlay titled ${String(pattern)} was opened and audited`,
          )
          .toBe(true);
      }
    }
  });

  // Non-admin personas: the Team editor of Ledger and a consumer (subscriber) see fewer areas
  // and collapsed access details; their pages must hold the same layout rules.
  for (const [prefix, persona] of [
    ["editor", LEDGER_EDITOR],
    ["subscriber", LEDGER_SUBSCRIBER],
  ] as const) {
    test(`the ${prefix} persona's pages at 1440, 1024 and 390`, async ({
      page,
    }) => {
      test.setTimeout(10 * 60_000);
      const folder = `/folders/${demo.ledger.id}`;
      for (const width of Object.keys(WIDTHS) as Width[]) {
        await page.setViewportSize({ width: WIDTHS[width], height: 900 });
        await page.context().clearCookies();
        await signInAs(page, persona);
        await audit(page, width, `${prefix} home`);
        await page.getByText("See details").first().click();
        await audit(page, width, `${prefix} home access details`);
        for (const url of [
          folder,
          `${folder}/contracts`,
          `${folder}/templates`,
          `${folder}/rule-sets`,
          `${folder}/cards`,
          `/contracts/${demo.orders.id}`,
          `/contracts/${demo.orders.id}/document`,
          `/contracts/${demo.orders.id}/versions`,
          "/rule-sets",
          "/cards",
        ]) {
          await page.goto(url);
          await audit(page, width, `${prefix} ${url}`);
        }
        await page.goto(`${folder}/access`);
        await audit(page, width, `${prefix} ledger access`);
        await page.getByText("See details").first().click();
        await audit(page, width, `${prefix} ledger access details`);
      }
    });
  }

  test("the shell's collapsed rail panel and phone drawer", async ({
    page,
  }) => {
    test.setTimeout(5 * 60_000);
    await page.setViewportSize({ width: 1024, height: 800 });
    await signIn(page);
    await page.goto(`/folders/${demo.ledger.id}`);
    await audit(page, "laptop", "shell panel open");
    await page.getByRole("button", { name: "Collapse navigation" }).click();
    await audit(page, "laptop", "shell panel collapsed");
    await page.goto("/contracts");
    await audit(page, "laptop", "shell panel collapsed contracts");
    await page.getByRole("button", { name: "Expand navigation" }).click();
    await audit(page, "laptop", "shell panel expanded again");
    await page.setViewportSize({ width: 390, height: 800 });
    await page.goto(`/folders/${demo.ledger.id}`);
    await audit(page, "phone", "shell top bar");
    await page.getByRole("button", { name: "Open navigation" }).click();
    await expect(
      page.getByRole("dialog", { name: "Navigation" }),
    ).toBeVisible();
    await audit(page, "phone", "shell drawer open", false);
  });
});
