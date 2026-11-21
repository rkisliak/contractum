// Switching users in one browser context must never show the previous user's data or errors:
// the query cache is dropped whenever the session starts or ends (src/auth/cacheReset.ts). For
// each ordered pair, A signs in and visits the main areas, signs out through the account menu, B
// signs in on the same page and visits the same areas: no ProblemAlert, no API error, B's own nav
// and B's own folders (only the admin sees Root). A sign-out in a second tab sends the first tab
// to /signin, and the next user signs in there cleanly.
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import {
  adminApi,
  adminPassword,
  findDemo,
  IssueCollector,
  visit,
  type Demo,
} from "./journey-helpers";
import {
  ensurePersonas,
  LEDGER_EDITOR,
  LEDGER_READER,
  LEDGER_SUBSCRIBER,
  signInAs,
  signOut,
  type Persona,
} from "./personas";

let demo: Demo;

test.use({ actionTimeout: 15_000, navigationTimeout: 30_000 });

test.beforeAll(async () => {
  const api = await adminApi();
  try {
    demo = await findDemo(api);
    await ensurePersonas(api);
  } finally {
    await api.dispose();
  }
});

type Who = { persona: Persona; admin: boolean };

const ADMIN = (): Who => ({
  admin: true,
  persona: {
    username: "admin",
    password: adminPassword(),
    displayName: "",
    folderPath: "",
    role: "global_admin",
  },
});
const as = (persona: Persona): Who => ({ persona, admin: false });

const PERSONA_NAV = [
  "Home",
  "Folders",
  "Contracts",
  "Rule sets",
  "Subscriptions",
];

async function signInWho(page: Page, who: Who): Promise<void> {
  if (!who.admin) {
    await signInAs(page, who.persona);
    return;
  }
  await page.goto("/signin");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password", { exact: true }).fill(adminPassword());
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: /^Welcome, / }),
  ).toBeVisible();
}

/** The main areas plus the Ledger tabs, each clean; then the identity checks for `who`. */
async function tour(page: Page, issues: IssueCollector, who: Who) {
  await visit(page, issues, "/", { h1: /^Welcome, / });
  const nav = page.getByRole("navigation", { name: "Primary" });
  const labels = (await nav.getByRole("link").allInnerTexts()).map((t) =>
    t.trim(),
  );
  if (who.admin) {
    expect(labels).toEqual(expect.arrayContaining(["Users", "Audit log"]));
  } else {
    expect(labels).toEqual(PERSONA_NAV);
  }
  await visit(page, issues, "/folders", { h1: "Folders" });
  await visit(page, issues, "/rule-sets", { h1: "Rule sets" });
  // Only the admin can see ROOT: it is the default folder and an option of the picker.
  const picker = page.getByLabel("Folder", { exact: true });
  const options = await picker.locator("option").allInnerTexts();
  if (who.admin) {
    expect(options.some((o) => o.startsWith("Root"))).toBe(true);
    await expect(
      page.getByRole("heading", { name: "Defined here", exact: true }),
    ).toBeVisible();
  } else {
    expect(options.some((o) => o.startsWith("Root"))).toBe(false);
    await expect(
      page.getByRole("heading", { name: "Defined here", exact: true }),
    ).toHaveCount(0);
  }
  await expect(
    page.getByRole("heading", { name: "Available here (inherited)" }),
  ).toBeVisible();
  await visit(page, issues, "/contracts", { h1: "Contracts" });
  await visit(page, issues, "/cards", { h1: "Subscriptions" });
  const folder = `/folders/${demo.ledger.id}`;
  await visit(page, issues, folder, { h1: "Ledger" });
  const tabs = await page
    .getByRole("navigation", { name: "Folder sections" })
    .getByRole("link")
    .evaluateAll((anchors) => anchors.map((a) => a.getAttribute("href") ?? ""));
  for (const href of tabs) await visit(page, issues, href, { h1: "Ledger" });
}

const PAIRS: [string, () => Who, () => Who][] = [
  ["reader then admin", () => as(LEDGER_READER), ADMIN],
  ["admin then reader", ADMIN, () => as(LEDGER_READER)],
  [
    "editor then subscriber",
    () => as(LEDGER_EDITOR),
    () => as(LEDGER_SUBSCRIBER),
  ],
  ["subscriber then admin", () => as(LEDGER_SUBSCRIBER), ADMIN],
];

for (const [name, first, second] of PAIRS) {
  test(`switching users in one tab: ${name}`, async ({ page }) => {
    test.setTimeout(300_000);
    const issues = new IssueCollector(page);
    const a = first();
    const b = second();
    await signInWho(page, a);
    await tour(page, issues, a);
    await signOut(page);
    await signInWho(page, b);
    await tour(page, issues, b);
  });
}

test("a sign-out in another tab ends this tab's session; the next user starts clean", async ({
  page,
  context,
}) => {
  test.setTimeout(300_000);
  const issues = new IssueCollector(page);
  const admin = ADMIN();
  await signInWho(page, admin);
  await visit(page, issues, "/rule-sets", { h1: "Rule sets" });
  const other = await context.newPage();
  await other.goto("/");
  await expect(
    other.getByRole("heading", { level: 1, name: /^Welcome, / }),
  ).toBeVisible();
  await signOut(other);
  await other.close();
  // The first tab hears the broadcast and leaves for the sign-in page.
  await expect(page).toHaveURL(/\/signin/, { timeout: 15_000 });
  issues.reset();
  const reader = as(LEDGER_READER);
  await signInWho(page, reader);
  await tour(page, issues, reader);
});
