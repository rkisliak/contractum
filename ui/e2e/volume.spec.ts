// The high-volume rendering audit (quick 261120-6i0): the seeded stack (`volume-seed.ts`) holds
// hundreds of records with 200-character, right-to-left, CJK and bidi-control text; every screen
// is measured with the layout rules (`layout-helpers.ts` R1 to R17), the issue collector (no
// console error, page error or API status 400 or above, no allowance) and axe. Run through the
// `volume` and `volume-dark` projects (see playwright.config.ts); screenshots go to
// `test-results/volume/<project>/<width>/`.
//
// A native `select` is measured as an element (inside its field, R2, R5, R6) and by its option
// count; the browser-drawn option list cannot be measured and is not.
import { mkdirSync } from "node:fs";
import { request, type Locator, type Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import {
  adminPassword,
  IssueCollector,
  signIn,
  softAxe,
} from "./journey-helpers";
import {
  audit,
  crawl,
  dismiss,
  nameOfOverlay,
  newContext,
  settle,
  visitAndAudit,
  WIDTHS,
  type CrawlContext,
  type Width,
} from "./layout-crawl";
import { OVERLAYS, POPUPS, ROUTES, VOLUME_ROUTES } from "./layout-coverage";
import { checkLayout } from "./layout-helpers";
import { signInAs, VOLUME_WANDERER } from "./personas";
import { Client } from "./seed-client";
import { findVolume, type Volume } from "./volume-seed";

let volume: Volume;
const visited = new Set<string>();
const popupsOpened = new Set<string>();

test.use({
  actionTimeout: 10_000,
  navigationTimeout: 30_000,
  reducedMotion: "reduce",
});

test.beforeAll(async () => {
  const ctx = await request.newContext({
    baseURL: process.env.DC_UI_E2E_SETUP_URL ?? "https://traefik",
    ignoreHTTPSErrors: true,
  });
  try {
    const login = await ctx.post("/api/v1/identity/auth/login", {
      data: { username: "admin", password: adminPassword() },
    });
    const { access_token } = (await login.json()) as { access_token: string };
    const api = new Client(ctx, access_token, "volume spec");
    volume = await findVolume(api);
  } finally {
    await ctx.dispose();
  }
});

/** One context per test: the hook checks the issue collector and axe after every state. */
function makeContext(page: Page): {
  ctx: CrawlContext;
  issues: IssueCollector;
} {
  const issues = new IssueCollector(page);
  const ctx = newContext(
    "volume",
    async (p, width, label, kind) => {
      expect
        .soft(issues.take(), `${label}: console, page or API errors`)
        .toEqual([]);
      // Routes: axe at every width. Overlay and popup states: axe at 1440 only (D-10).
      if (kind === "route" || width === "desktop") await softAxe(p, label);
    },
    true,
  );
  return { ctx, issues };
}

/** Runs a step; a failure is one soft assertion naming the step, so the run goes on. */
async function attempt(label: string, fn: () => Promise<void>): Promise<void> {
  try {
    await test.step(label, fn);
  } catch (error) {
    expect.soft(String(error).slice(0, 600), `step failed: ${label}`).toBe("");
    // Leave nothing open for the next step.
  }
}

function allUrls(): { route: string; url: string }[] {
  const out: { route: string; url: string }[] = [];
  for (const [route, make] of Object.entries(VOLUME_ROUTES)) {
    for (const url of make(volume)) {
      expect
        .soft(url, `${route}: the volume data has no record for it`)
        .toBeTruthy();
      if (url) out.push({ route, url });
    }
  }
  return out;
}

/** The page never scrolls horizontally (a soft assertion, so one run reports every page). */
async function softNoHorizontalOverflow(
  page: Page,
  label: string,
): Promise<void> {
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect
    .soft(scrollWidth, `${label}: document scrollWidth vs clientWidth`)
    .toBeLessThanOrEqual(clientWidth);
}

/** Every table fits its wrapper or sits in a horizontal scroll container inside the main region. */
async function checkTables(page: Page, label: string): Promise<void> {
  const problems = await page.evaluate(() => {
    const out: string[] = [];
    const main = document.querySelector("main")?.getBoundingClientRect();
    for (const table of document.querySelectorAll("main table")) {
      const t = table.getBoundingClientRect();
      if (t.width < 2) continue;
      let scroller: Element | null = null;
      for (let p = table.parentElement; p; p = p.parentElement) {
        const ox = getComputedStyle(p).overflowX;
        if (ox === "auto" || ox === "scroll") {
          scroller = p;
          break;
        }
      }
      const box = (scroller ?? table.parentElement)?.getBoundingClientRect();
      if (!box || !main) continue;
      if (scroller) {
        if (box.right > main.right + 1 || box.left < main.left - 1)
          out.push(
            `table scroll region spans ${String(Math.round(box.left))}..${String(Math.round(box.right))}px outside main ${String(Math.round(main.left))}..${String(Math.round(main.right))}px`,
          );
      } else if (t.right > box.right + 1 || t.left < box.left - 1) {
        out.push(
          `table spans ${String(Math.round(t.left))}..${String(Math.round(t.right))}px outside its wrapper ${String(Math.round(box.left))}..${String(Math.round(box.right))}px`,
        );
      }
    }
    return out;
  });
  expect.soft(problems, `${label}: table containment`).toEqual([]);
}

/** A route: visit, audit (layout, issues, axe), overflow and tables, screenshot. */
async function routeState(
  ctx: CrawlContext,
  issues: IssueCollector,
  page: Page,
  width: Width,
  url: string,
): Promise<void> {
  issues.reset();
  await visitAndAudit(ctx, page, width, url);
  await softNoHorizontalOverflow(page, `${url} @${String(WIDTHS[width])}`);
  await checkTables(page, `${url} @${String(WIDTHS[width])}`);
}

// ---------- popups ----------

/** Types `vo` into a combobox of the current scope and audits the listbox it opens. */
async function typeIntoCombobox(
  ctx: CrawlContext,
  page: Page,
  width: Width,
  url: string,
  box: Locator,
  text: string,
): Promise<{ options: number; narrow: boolean } | null> {
  const label =
    (await box.getAttribute("aria-label")) ??
    (await box.evaluate(
      (el) =>
        (el.id &&
          document.querySelector(`label[for="${CSS.escape(el.id)}"]`)
            ?.textContent) ||
        "",
    ));
  if ((await box.evaluate((el) => el.tagName)) === "SELECT") return null;
  await box.fill(text);
  await page.waitForTimeout(600);
  const listbox = page.getByRole("listbox").filter({ visible: true }).first();
  if (!(await listbox.isVisible().catch(() => false))) {
    await box.fill("");
    return null;
  }
  const options = await listbox.getByRole("option").count();
  const narrow = await page
    .getByText("Type more to narrow the list")
    .first()
    .isVisible()
    .catch(() => false);
  popupsOpened.add(label.trim());
  ctx.opened.add(`popup ${label.trim()}`);
  await audit(ctx, page, width, `${url} popup ${label.trim()} ${text}`, false);
  expect
    .soft(options, `${url}: "${label.trim()}" lists at most 20 options`)
    .toBeLessThanOrEqual(20);
  if (options >= 20)
    expect
      .soft(
        narrow,
        `${url}: "${label.trim()}" says "Type more to narrow the list"`,
      )
      .toBe(true);
  await box.fill("");
  await page.keyboard.press("Escape");
  return { options, narrow };
}

/** The generic popup pass on the page and on the dialog that is open now. */
async function popupPass(
  ctx: CrawlContext,
  page: Page,
  width: Width,
  url: string,
): Promise<void> {
  const scope = page
    .locator("[role=dialog]")
    .filter({ visible: true })
    .last()
    .or(page.locator("main"))
    .first();
  const boxes = scope.getByRole("combobox").filter({ visible: true });
  const n = await boxes.count();
  for (let i = 0; i < n; i++) {
    await typeIntoCombobox(ctx, page, width, url, boxes.nth(i), "vo");
  }
}

// ---------- explicit states (D-11) ----------

async function openButton(page: Page, name: string | RegExp): Promise<void> {
  await page
    .locator("main")
    .getByRole("button", { name })
    .filter({ visible: true })
    .first()
    .click();
}

async function explicitStates(
  ctx: CrawlContext,
  issues: IssueCollector,
  page: Page,
  width: Width,
): Promise<void> {
  const goto = async (url: string) => {
    issues.reset();
    await page.goto(url);
    await settle(page);
  };
  const a = (label: string, full = false) =>
    audit(ctx, page, width, label, full);

  // --- pickers: 300+ matches, at most 20 options and "Type more to narrow the list" ---
  await attempt("Grant access picker on the access-heavy team", async () => {
    await goto(`/folders/${volume.accessTeam.id}/access`);
    await openButton(page, "Grant access");
    const dialog = page.getByRole("dialog", { name: "Grant access" });
    await expect(dialog).toBeVisible();
    const types = await dialog
      .getByLabel("Principal type")
      .locator("option")
      .allInnerTexts();
    for (const type of types.slice(0, 6)) {
      await dialog.getByLabel("Principal type").selectOption({ label: type });
      await typeIntoCombobox(
        ctx,
        page,
        width,
        "grant access",
        dialog.getByRole("combobox").last(),
        "vo",
      );
    }
    await a("grant access dialog");
    await dismiss(page);
  });
  await attempt("Add member picker on the 120-member group", async () => {
    await goto(`/admin/groups/${volume.group120.id}`);
    await openButton(page, "Add member");
    const dialog = page.getByRole("dialog").last();
    await expect(dialog).toBeVisible();
    const res = await typeIntoCombobox(
      ctx,
      page,
      width,
      "group add member",
      dialog.getByRole("combobox").last(),
      "vo",
    );
    expect
      .soft(res?.options ?? 0, "group member picker offers options")
      .toBeGreaterThan(0);
    await dismiss(page);
  });
  await attempt("Add member picker on a card", async () => {
    await goto(`/cards/${volume.card20.id}?tab=members`);
    await openButton(page, "Add member");
    const dialog = page.getByRole("dialog").last();
    await expect(dialog).toBeVisible();
    await typeIntoCombobox(
      ctx,
      page,
      width,
      "card add member",
      dialog.getByRole("combobox").last(),
      "vo",
    );
    await dismiss(page);
  });
  for (const [url, menu, item, label, term] of [
    [
      "/admin/groups",
      "New",
      "Register Entra group",
      "Search Entra groups",
      "Volume Entra",
    ],
    [
      "/admin/users",
      "New",
      "Register Entra user",
      "Search Entra users",
      "Volume Entra",
    ],
    [
      "/admin/service-accounts",
      "New",
      "Register Entra service principal",
      "Search Entra service principals",
      "Volume Entra",
    ],
  ] as const) {
    await attempt(`${item} with a generated-prefix search`, async () => {
      await goto(url);
      await page.getByRole("button", { name: menu }).first().click();
      await page.getByRole("menuitem", { name: item }).click();
      const dialog = page.getByRole("dialog", { name: item });
      await expect(dialog).toBeVisible();
      const box = dialog.getByRole("combobox", { name: label });
      await typeIntoCombobox(ctx, page, width, item, box, term);
      await box.fill("volume");
      await page.waitForTimeout(700);
      await a(`${item} dialog searched`);
      await dismiss(page);
    });
  }

  // --- selects and switchers ---
  await attempt("Switch folder with 40+ folders", async () => {
    await goto(`/folders/${volume.longTeam.id}`);
    // "Switch folder" is a native select: measured as an element and by its option count (the
    // browser-drawn option list itself cannot be measured).
    const box = page.getByLabel("Switch folder");
    const options = await box.locator("option").count();
    expect(
      options,
      "Switch folder lists the 40 volume folders",
    ).toBeGreaterThanOrEqual(40);
    await box.focus();
    await a("switch folder select");
  });
  await attempt("Subscribe and Move pin dialogs", async () => {
    await goto(`/cards/${volume.card20.id}?tab=subscriptions`);
    const subscribe = page
      .locator("main")
      .getByRole("button", { name: /^Subscribe/ })
      .first();
    if (await subscribe.isVisible().catch(() => false)) {
      await subscribe.click();
      const dialog = page.getByRole("dialog", {
        name: "Subscribe to a contract",
      });
      await expect(dialog).toBeVisible();
      await popupPass(ctx, page, width, "subscribe");
      await a("subscribe dialog");
      await dismiss(page);
    }
    const move = page.getByRole("button", { name: /^Move pin / }).first();
    if (await move.isVisible().catch(() => false)) {
      await move.click();
      await a("move pin dialog");
      await dismiss(page);
    }
  });
  await attempt("New contract and Create card folder selects", async () => {
    await goto("/contracts/new");
    await a("new contract folder select", true);
    await goto("/cards");
    await openButton(page, /^Create card/);
    await a("create card dialog");
    await dismiss(page);
  });

  // --- menus and confirms ---
  await attempt("version Actions on the 25-version contract", async () => {
    await goto(`/contracts/${volume.bigContract.id}/versions`);
    const actions = page
      .locator("main")
      .getByRole("button", { name: /^Actions for / })
      .filter({ visible: true });
    const count = await actions.count();
    for (let i = 0; i < Math.min(count, 3); i++) {
      await actions.nth(i).click();
      await a(`version actions menu ${String(i)}`);
      const items = page.getByRole("menuitem");
      const total = await items.count();
      for (let j = 0; j < total; j++) {
        const item = items.nth(j);
        const text = (await item.innerText()).trim();
        if (!/^(promote|unpublish|deprecate|republish|delete)/i.test(text))
          continue;
        if (await item.isDisabled().catch(() => true)) continue;
        await item.click();
        const dlg = page
          .locator("[role=dialog], [role=alertdialog]")
          .filter({ visible: true })
          .last();
        if (await dlg.isVisible().catch(() => false)) {
          ctx.opened.add(await nameOfOverlay(dlg));
          await a(`version action ${text}`);
        }
        await dismiss(page);
        await actions.nth(i).click();
      }
      await dismiss(page);
    }
  });
  await attempt(
    "folder Actions, Delete, Archive and Rename on the long team",
    async () => {
      await goto(`/folders/${volume.longTeam.id}`);
      const actions = page
        .getByRole("button", { name: /^Actions for / })
        .filter({ visible: true })
        .first();
      await actions.click();
      ctx.opened.add("menu Actions for folder");
      await a("folder actions menu");
      const del = page.getByRole("menuitem", { name: /^Delete folder/ });
      if (await del.isVisible().catch(() => false)) {
        await del.hover();
        await page.waitForTimeout(500);
        await a("delete folder blockers tooltip");
        if (!(await del.isDisabled().catch(() => true))) {
          await del.click();
          await a("delete folder dialog");
          await dismiss(page);
        } else await page.keyboard.press("Escape");
      }
      for (const name of [/^Archive/, /^Rename/]) {
        await actions.click();
        const item = page.getByRole("menuitem", { name }).first();
        if (!(await item.isVisible().catch(() => false))) {
          await page.keyboard.press("Escape");
          continue;
        }
        await item.click();
        const dlg = page
          .locator("[role=dialog], [role=alertdialog]")
          .filter({ visible: true })
          .last();
        if (await dlg.isVisible().catch(() => false)) {
          ctx.opened.add(await nameOfOverlay(dlg));
          await a(`long team ${String(name)} dialog`);
        }
        await dismiss(page);
      }
    },
  );
  await attempt("Publish dialog of a long-named draft", async () => {
    await goto(
      `/contracts/${volume.draftContract.id}/drafts/${volume.draftContract.draftId}`,
    );
    const publish = page
      .getByRole("button", { name: /^Publish/ })
      .filter({ visible: true })
      .first();
    await publish.click();
    const dlg = page
      .locator("[role=dialog], [role=alertdialog]")
      .filter({ visible: true })
      .last();
    await expect(dlg).toBeVisible();
    ctx.opened.add(await nameOfOverlay(dlg));
    await a("publish dialog of a long-named draft");
    await dismiss(page);
  });
  await attempt("the account menu", async () => {
    await goto("/");
    const account = page
      .getByRole("button", { name: /account menu/i })
      .filter({ visible: true })
      .first();
    await account.click();
    ctx.opened.add("menu account");
    await a("account menu");
    await dismiss(page);
  });

  await attempt(
    "the mandatory confirm of the 20-version rule set",
    async () => {
      await goto(`/rule-sets/${volume.ruleSet20.id}?tab=rollout`);
      await page.locator("#rule-set-mandatory").click({ force: true });
      const dlg = page
        .locator("[role=dialog], [role=alertdialog]")
        .filter({ visible: true })
        .last();
      await expect(dlg).toBeVisible();
      ctx.opened.add(await nameOfOverlay(dlg));
      await a("make mandatory confirm of a long-named rule set");
      await dismiss(page);
    },
  );
  await attempt("the navigation drawer and the Versions dialog", async () => {
    await goto("/");
    if (width === "phone") {
      await page.getByRole("button", { name: "Open navigation" }).click();
      const drawer = page.getByRole("dialog", { name: "Navigation" });
      await expect(drawer).toBeVisible();
      ctx.opened.add("Navigation");
      await a("navigation drawer");
      await dismiss(page);
    }
    const account = page
      .getByRole("button", { name: /account menu/i })
      .filter({ visible: true })
      .first();
    await account.click();
    await page.getByRole("menuitem", { name: "Versions" }).click();
    const versions = page.getByRole("dialog", { name: "Versions" });
    await expect(versions).toBeVisible();
    await expect(versions.getByText("Loading…")).toHaveCount(0);
    ctx.opened.add("Versions");
    await a("versions dialog");
    await dismiss(page);
  });

  // --- lists: Load more twice, then audited ---
  for (const url of [
    "/admin/users",
    "/admin/groups",
    "/contracts",
    "/cards",
    "/audit",
    "/account/tokens",
  ]) {
    await attempt(`${url} Load more twice`, async () => {
      await goto(url);
      for (let i = 0; i < 2; i++) {
        const more = page
          .getByRole("button", { name: /^Load more/ })
          .filter({ visible: true })
          .first();
        if (!(await more.isVisible().catch(() => false))) break;
        await more.click();
        await settle(page);
      }
      // The phone's sticky top bar would sit over scrolled rows (R10): measure from the top.
      await page.evaluate(() => {
        window.scrollTo(0, 0);
        document.getElementById("main-content")?.scrollTo(0, 0);
      });
      await a(`${url} after load more`, true);
      await checkTables(
        page,
        `${url} after load more @${String(WIDTHS[width])}`,
      );
    });
  }

  // --- the audit drawer, /folders expanded, a 100-character token ---
  await attempt("audit event drawer", async () => {
    await goto("/audit");
    await page
      .getByRole("table")
      .locator("tbody tr")
      .first()
      .getByRole("button")
      .first()
      .click();
    const drawer = page.getByRole("dialog").last();
    await expect(drawer).toBeVisible();
    ctx.opened.add(await nameOfOverlay(drawer));
    await a("audit event drawer");
    await dismiss(page);
  });
  await attempt("/folders with every row expanded", async () => {
    await goto("/folders");
    const tree = page.getByRole("treegrid", { name: "All folders" });
    for (let i = 0; i < 4; i++) {
      const closed = tree.locator("[role=row][aria-expanded=false]");
      const n = await closed.count();
      for (let j = 0; j < n; j++)
        await closed.first().locator("[data-tree-toggle]").click();
    }
    await page.evaluate(() => {
      window.scrollTo(0, 0);
      document.getElementById("main-content")?.scrollTo(0, 0);
    });
    await a("/folders expanded", true);
  });
  // The tree with contracts: every folder expanded, so the volume contracts (200-character,
  // Arabic, Hebrew, CJK and emoji names, a 255-character ODCS id) sit in tree rows.
  await attempt("/folders?contracts=true with every row expanded", async () => {
    await goto("/folders?contracts=true");
    const tree = page.getByRole("treegrid", { name: "All folders" });
    for (let i = 0; i < 4; i++) {
      const closed = tree.locator("[role=row][aria-expanded=false]");
      const n = await closed.count();
      for (let j = 0; j < n; j++)
        await closed.first().locator("[data-tree-toggle]").click();
    }
    await expect(page.locator("[aria-busy='true']")).toHaveCount(0, {
      timeout: 30_000,
    });
    await page.evaluate(() => {
      window.scrollTo(0, 0);
      document.getElementById("main-content")?.scrollTo(0, 0);
    });
    await a("/folders?contracts=true expanded", true);
  });
  await attempt("a token with a 100-character name", async () => {
    await goto("/account/tokens");
    const tokenName = `volspec-${Date.now().toString(36)}`.padEnd(100, "t");
    await page.getByRole("button", { name: "Create token" }).click();
    const create = page.getByRole("dialog", { name: "Create token" });
    await create.getByLabel("Name").fill(tokenName);
    await create.getByRole("button", { name: "Create token" }).click();
    const reveal = page.getByRole("dialog", { name: /^Your new / });
    await expect(reveal).toBeVisible();
    ctx.opened.add(await nameOfOverlay(reveal));
    await a("secret reveal dialog with a 100-character name");
    await reveal.getByLabel(/^I have stored this /).check();
    await reveal.getByRole("button", { name: /^Close after storing / }).click();
    await expect(reveal).toBeHidden();
    const row = page.getByRole("row", {
      name: new RegExp(tokenName.slice(0, 20)),
    });
    // The new token may sit on a later page of the list.
    for (
      let i = 0;
      i < 6 &&
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
    ctx.opened.add(await nameOfOverlay(revoke));
    await a("revoke token confirm with a 100-character name");
    await revoke.getByRole("button", { name: /^Revoke / }).click();
    await expect(revoke).toBeHidden();
  });
}

test.describe("volume audit", () => {
  test("tracer: the long-named team at 1440", async ({ page }) => {
    test.setTimeout(5 * 60_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    await signIn(page);
    const issues = new IssueCollector(page);
    issues.reset();
    const url = `/folders/${volume.longTeam.id}/contracts`;
    await page.goto(url);
    await page.getByRole("heading", { level: 1 }).first().waitFor();
    await page.waitForLoadState("networkidle").catch(() => undefined);
    await checkLayout(page, `${url} @1440`);
    expect
      .soft(issues.take(), `${url}: console, page or API errors`)
      .toEqual([]);
    await softAxe(page, url);
    const dir = `test-results/volume/${test.info().project.name}/desktop`;
    mkdirSync(dir, { recursive: true });
    await page.screenshot({
      path: `${dir}/tracer-long-named-team.png`,
      fullPage: true,
    });
  });

  test("volume: /signin and every route as admin at 1440, 1024 and 390", async ({
    page,
  }) => {
    test.setTimeout(120 * 60_000);
    const { ctx, issues } = makeContext(page);
    for (const width of Object.keys(WIDTHS) as Width[]) {
      await page.setViewportSize({ width: WIDTHS[width], height: 900 });
      await page.context().clearCookies();
      issues.reset();
      await page.goto("/signin");
      await audit(ctx, page, width, "/signin", true, "route");
      await signIn(page);
      for (const { route, url } of allUrls()) {
        await routeState(ctx, issues, page, width, url);
        visited.add(route);
      }
    }
    for (const route of Object.keys(ROUTES))
      expect.soft(visited.has(route), `route ${route} was audited`).toBe(true);
  });

  test("volume: every dialog, menu, drawer, tab and popup as admin at 1440 and 390", async ({
    page,
  }) => {
    test.setTimeout(240 * 60_000);
    const { ctx, issues } = makeContext(page);
    for (const width of ["desktop", "phone"] as Width[]) {
      await page.setViewportSize({ width: WIDTHS[width], height: 900 });
      await page.context().clearCookies();
      await signIn(page);
      for (const { url } of allUrls()) {
        issues.reset();
        await page.goto(url);
        await settle(page);
        await crawl(ctx, page, width, url);
        await popupPass(ctx, page, width, url);
      }
      await explicitStates(ctx, issues, page, width);
    }
    console.log(`overlays opened:\n${[...ctx.opened].sort().join("\n")}`);
    for (const [file, patterns] of Object.entries(OVERLAYS)) {
      for (const pattern of patterns) {
        expect
          .soft(
            [...ctx.opened].some((t) => pattern.test(t)),
            `${file}: an overlay titled ${String(pattern)} was opened and audited`,
          )
          .toBe(true);
      }
    }
    for (const [file, patterns] of Object.entries(POPUPS)) {
      // "Show access at" is the wanderer's (the next test).
      if (file.endsWith("EffectiveAccess.tsx")) continue;
      for (const pattern of patterns) {
        expect
          .soft(
            [...popupsOpened].some((t) => pattern.test(t)),
            `${file}: a popup named ${String(pattern)} was opened and audited`,
          )
          .toBe(true);
      }
    }
  });

  test("volume: the wanderer's pages at 1440, 1024 and 390", async ({
    page,
  }) => {
    test.setTimeout(60 * 60_000);
    const { ctx, issues } = makeContext(page);
    const folder = `/folders/${volume.longTeam.id}`;
    const big = `/contracts/${volume.bigContract.id}`;
    for (const width of Object.keys(WIDTHS) as Width[]) {
      await page.setViewportSize({ width: WIDTHS[width], height: 900 });
      await page.context().clearCookies();
      issues.reset();
      await signInAs(page, VOLUME_WANDERER);
      await audit(ctx, page, width, "wanderer home", true, "route");
      await page.getByText("See details").first().click();
      const select = page.getByLabel("Show access at");
      await expect(select).toBeVisible();
      const options = await select.locator("option").allInnerTexts();
      expect(
        options.length,
        "Show access at lists 24 or more folders",
      ).toBeGreaterThanOrEqual(24);
      const picks = [
        options[1],
        options.find((o) => o.startsWith("VolumeLongNamedTeam")),
        options.find((o) => o.includes(" فريق")),
      ];
      for (const pick of picks) {
        if (!pick) continue;
        await select.selectOption({ label: pick });
        await expect(
          page.getByText(/What you can do at .* and below/).first(),
        ).toContainText(pick.slice(0, 20));
        await audit(
          ctx,
          page,
          width,
          `wanderer access at ${pick.slice(0, 24)}`,
        );
        popupsOpened.add("Show access at");
      }
      const account = page
        .getByRole("button", { name: /account menu/i })
        .filter({ visible: true })
        .first();
      await account.click();
      await audit(ctx, page, width, "wanderer account menu", false);
      await dismiss(page);
      for (const url of [
        folder,
        `${folder}/contracts`,
        `${folder}/cards`,
        `${folder}/rule-sets`,
        `${folder}/access`,
        big,
        `${big}/document`,
        `${big}/versions`,
        `${big}/diff?from=1.0.0&to=1.24.0`,
        "/contracts",
        "/cards",
        "/rule-sets",
        "/account/profile",
        "/account/tokens",
      ]) {
        issues.reset();
        await page.goto(url);
        await expect(
          page.getByRole("heading", { level: 1 }).first(),
        ).toBeVisible({ timeout: 15_000 });
        await audit(ctx, page, width, `wanderer ${url}`, true, "route");
        await checkTables(page, `wanderer ${url} @${String(WIDTHS[width])}`);
      }
    }
    for (const [file, patterns] of Object.entries(POPUPS)) {
      if (!file.endsWith("EffectiveAccess.tsx")) continue;
      for (const pattern of patterns)
        expect
          .soft(
            [...popupsOpened].some((t) => pattern.test(t)),
            `${file}: a popup named ${String(pattern)} was opened and audited`,
          )
          .toBe(true);
    }
  });
});
