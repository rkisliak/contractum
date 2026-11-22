// The UI as non-admin users against the real stack (no mocks): the Team editor and the Team reader
// of Acme > Payments > Ledger (e2e/personas.ts, recreated idempotently). Every area the persona is
// shown (sidebar entries, the Ledger folder and each of its tabs, the Orders contract and each of
// its tabs, the account pages) must render an h1 with no ProblemAlert, no console or page error,
// no API error of any status (nothing is allow-listed) and no serious axe violation. Admin-only
// navigation must be absent, and "Your access" on Home must stay collapsed until "See details".
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import {
  adminApi,
  expectCleanPage,
  findDemo,
  IssueCollector,
  visit,
  type Demo,
} from "./journey-helpers";
import {
  ACME_ORG_ADMIN,
  ensurePersonas,
  LEDGER_EDITOR,
  LEDGER_READER,
  LEDGER_SUBSCRIBER,
  openAccountMenu,
  signInAs,
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

/** Entries only an administrator (of some scope) can use; never shown to these personas. */
const ADMIN_ONLY = [
  "Templates",
  "Dry runs",
  "Drift",
  "Audit log",
  "Users",
  "Groups",
  "Service accounts",
  "All access tokens",
  "Settings",
];

const EXPECTED_NAV: Record<string, string[]> = {
  [LEDGER_EDITOR.username]: [
    "Home",
    "Folders",
    "Contracts",
    "Rule sets",
    "Subscriptions",
  ],
  [LEDGER_READER.username]: [
    "Home",
    "Folders",
    "Contracts",
    "Rule sets",
    "Subscriptions",
  ],
  [LEDGER_SUBSCRIBER.username]: [
    "Home",
    "Folders",
    "Contracts",
    "Rule sets",
    "Subscriptions",
  ],
};

/** Cards the subscriber journey creates in Ledger are named with this prefix. */
const CARD_PREFIX = "Subscriber journey ";

/** Archives every active "Subscriber journey ..." card left in Ledger and Acme (as admin). */
async function cleanupSubscriberCards(): Promise<void> {
  const api = await adminApi();
  try {
    for (const folder of [demo.ledger, demo.acme]) {
      const cards = await api.get<{
        items: { id: string; name: string; archived_at?: string | null }[];
      }>(`contracts/folders/${folder.id}/cards`);
      for (const card of cards.items)
        if (card.name.startsWith(CARD_PREFIX) && !card.archived_at)
          await api.post(`contracts/cards/${card.id}/archive`);
    }
  } finally {
    await api.dispose();
  }
}

async function tabHrefs(page: Page, nav: string): Promise<string[]> {
  const links = page.getByRole("navigation", { name: nav }).getByRole("link");
  await expect(links.first()).toBeVisible();
  const hrefs = await links.evaluateAll((anchors) =>
    anchors.map((a) => a.getAttribute("href") ?? ""),
  );
  return [...new Set(hrefs.filter((href) => href.startsWith("/")))];
}

async function journey(page: Page, persona: Persona) {
  const issues = new IssueCollector(page);
  await signInAs(page, persona);
  await visit(page, issues, "/", { h1: `Welcome, ${persona.displayName}` });

  // Navigation: exactly the areas the persona can use.
  const nav = page.getByRole("navigation", { name: "Primary" });
  const labels = (await nav.getByRole("link").allInnerTexts()).map((t) =>
    t.trim(),
  );
  expect(labels).toEqual(EXPECTED_NAV[persona.username]);
  for (const label of ADMIN_ONLY)
    await expect(
      nav.getByRole("link", { name: label, exact: true }),
    ).toHaveCount(0);
  // The account menu (the rail's avatar) names the folder where the role is held.
  await openAccountMenu(page);
  await expect(page.getByRole("menu").getByText(/· Ledger$/)).toBeVisible();
  // "Versions" leads with the platform version and lists every component with its build; every
  // component answers, all with the same version (one build of the stack).
  await page.getByRole("menuitem", { name: "Versions" }).click();
  const versions = page.getByRole("dialog", { name: "Versions" });
  await expect(versions.getByText(/^Contractum \S+$/)).toBeVisible();
  for (const name of [
    "Web UI",
    "Identity",
    "Contracts",
    "Validation",
    "Audit",
    "API docs",
  ])
    await expect(versions.getByRole("rowheader", { name })).toBeVisible();
  await expect(versions.getByText("Loading…")).toHaveCount(0);
  await expect(versions.getByText("Unavailable")).toHaveCount(0);
  await expect(versions.getByText("Different version")).toHaveCount(0);
  await versions.getByRole("button", { name: "Close", exact: true }).click();
  await expectCleanPage(page, issues, "versions dialog", {
    h1: `Welcome, ${persona.displayName}`,
  });

  // "Your access" stays collapsed: only the role line and "See details".
  const access = page
    .getByRole("heading", { name: "Your access" })
    .locator("xpath=ancestor::section[1]");
  await expect(access.getByText("See details")).toBeVisible();
  await expect(access.getByRole("table")).toBeHidden();
  await expect(access.getByText(/^No access:/)).toBeHidden();
  await expect(
    access.getByText("Show technical permission names"),
  ).toBeHidden();
  await access.getByText("See details").click();
  await expect(access.getByRole("table")).toBeVisible();
  // Quick links only into areas the persona can use.
  const quick = page
    .getByRole("heading", { name: "Quick links" })
    .locator("xpath=ancestor::section[1]");
  await expect(quick.getByRole("link", { name: /Audit log/ })).toHaveCount(0);
  await expect(quick.getByRole("link", { name: /Rule sets/ })).toBeVisible();

  // Every sidebar entry.
  const navHrefs = await nav
    .getByRole("link")
    .evaluateAll((anchors) => anchors.map((a) => a.getAttribute("href") ?? ""));
  for (const href of navHrefs.filter((h) => h !== "/"))
    await visit(page, issues, href, { h1: /.+/ });

  // The Ledger folder and every tab it shows.
  const folder = `/folders/${demo.ledger.id}`;
  await visit(page, issues, folder, { h1: "Ledger" });
  for (const href of await tabHrefs(page, "Folder sections"))
    await visit(page, issues, href, { h1: "Ledger" });

  // Only a Global admin deletes folders: the actions menu, when this persona has one, never
  // offers it (and the deletion check is never requested).
  await visit(page, issues, folder, { h1: "Ledger" });
  const actions = page.getByRole("button", { name: "Actions for Ledger" });
  if ((await actions.count()) > 0) {
    await actions.click();
    await expect(
      page.getByRole("menuitem", { name: "Delete folder…" }),
    ).toHaveCount(0);
    await page.keyboard.press("Escape");
  }

  // The folder Access tab: the partial-access details only behind "See details".
  await visit(page, issues, `${folder}/access`, { h1: "Ledger" });
  const effective = page
    .getByRole("heading", { name: "Your effective access" })
    .locator("xpath=ancestor::section[1]");
  await expect(effective.getByRole("table")).toBeHidden();
  await effective.getByText("See details").click();
  await expect(effective.getByRole("table")).toBeVisible();
  await expectCleanPage(page, issues, "access details", { h1: "Ledger" });

  // A contract in Ledger and every tab it shows.
  const contract = `/contracts/${demo.orders.id}`;
  await visit(page, issues, contract, { h1: demo.orders.name });
  for (const href of await tabHrefs(page, "Contract sections"))
    await visit(page, issues, href, { h1: demo.orders.name });
  await visit(page, issues, `${contract}/versions/1.0.0`, {
    h1: demo.orders.name,
  });

  // Rule sets inherited at Ledger open read-only through the folder (`via`).
  await visit(page, issues, `${folder}/rule-sets`, { h1: "Ledger" });
  const inherited = page
    .getByRole("region", { name: "Available here (inherited)" })
    .getByRole("link")
    .first();
  if ((await inherited.count()) > 0) {
    const href = (await inherited.getAttribute("href")) ?? "";
    await visit(page, issues, href, { h1: /.+/ });
  }

  // Account pages.
  await visit(page, issues, "/account/password", { h1: "Change password" });
  await visit(page, issues, "/account/tokens", { h1: "Access tokens" });
}

for (const persona of [LEDGER_EDITOR, LEDGER_READER, LEDGER_SUBSCRIBER]) {
  test(`${persona.username}: every reachable area renders cleanly`, async ({
    page,
  }) => {
    test.setTimeout(600_000);
    await journey(page, persona);
  });
}

// The consumer's core flow, end to end through the UI: create a subscriber card in Ledger,
// subscribe it to the published Orders version, see the pin listed, then archive the card.
// Everything a consumer may not do (drafts, editing, publishing, deleting, grants) is hidden.
test.describe("subscriber flow", () => {
  test.beforeAll(cleanupSubscriberCards);
  test.afterAll(cleanupSubscriberCards);

  test("ledger-subscriber creates, subscribes and archives a card", async ({
    page,
  }) => {
    test.setTimeout(300_000);
    const issues = new IssueCollector(page);
    await signInAs(page, LEDGER_SUBSCRIBER);

    // The published contract offers only reading: no drafts, edit, publish or delete.
    const contract = `/contracts/${demo.orders.id}`;
    await visit(page, issues, contract, { h1: demo.orders.name });
    const main = page.getByRole("main");
    for (const name of [
      /New draft/,
      /Open drafts/,
      /Delete contract/,
      /Publish/,
      /Attach rule set/,
    ])
      await expect(main.getByRole("button", { name })).toHaveCount(0);
    await expect(main.getByRole("link", { name: /Open drafts/ })).toHaveCount(
      0,
    );
    const sections = page.getByRole("navigation", {
      name: "Contract sections",
    });
    await expect(sections.getByRole("link", { name: "Drafts" })).toHaveCount(0);
    await expect(
      sections.getByRole("link", { name: "Subscribers" }),
    ).toHaveCount(0);
    await visit(page, issues, `${contract}/versions`, { h1: demo.orders.name });
    for (const name of [/Promote/, /Unpublish/, /Deprecate/, /Republish/])
      await expect(main.getByRole("button", { name })).toHaveCount(0);

    // No grant controls on the Access tab.
    await visit(page, issues, `/folders/${demo.ledger.id}/access`, {
      h1: "Ledger",
    });
    await expect(
      main.getByRole("button", { name: /Grant access/ }),
    ).toHaveCount(0);
    await expect(
      main.getByRole("button", { name: /inheritance/i }),
    ).toHaveCount(0);

    // Create a card from the Ledger Subscribers tab.
    await visit(page, issues, `/folders/${demo.ledger.id}/cards`, {
      h1: "Ledger",
    });
    const cardName = `${CARD_PREFIX}${Date.now().toString(36)}`;
    await main.getByRole("button", { name: "Create card" }).first().click();
    const create = page.getByRole("dialog", { name: /card/i });
    await expectCleanPage(page, issues, "create card dialog", {
      h1: "Ledger",
      dialog: true,
    });
    await page.screenshot({
      path: "test-results/subscriber/create-card-dialog.png",
    });
    await create.getByLabel("Name").fill(cardName);
    await create
      .getByLabel("Description")
      .fill("The e2e consumer team (archived by the journey).");
    await create.getByRole("button", { name: "Create card" }).click();
    await expect(
      page.getByRole("heading", { level: 1, name: cardName }),
    ).toBeVisible();
    await expectCleanPage(page, issues, "new card", { h1: cardName });

    // Every card tab renders cleanly.
    for (const tab of [/^Overview$/, /^Members/, /^Notices$/, /^Webhook$/]) {
      const control = page.getByRole("tab", { name: tab });
      if ((await control.count()) === 0) continue;
      issues.reset();
      await control.click();
      await expectCleanPage(page, issues, `card tab ${String(tab)}`, {
        h1: cardName,
      });
    }

    // Subscribe it to the published Orders version.
    issues.reset();
    await page.getByRole("tab", { name: "Subscriptions" }).click();
    await main
      .getByRole("button", { name: "Subscribe to a contract" })
      .first()
      .click();
    const subscribe = page.getByRole("dialog", {
      name: "Subscribe to a contract",
    });
    await subscribe.getByLabel("Find a contract").fill(demo.orders.name);
    await subscribe
      .getByRole("list", { name: "Matching contracts" })
      .getByRole("button", { name: new RegExp(demo.orders.name) })
      .first()
      .click();
    await expect(subscribe.getByLabel("Version")).not.toHaveValue("");
    await expectCleanPage(page, issues, "subscribe dialog", {
      h1: cardName,
      dialog: true,
    });
    await subscribe.getByRole("button", { name: "Subscribe card" }).click();
    await expect(subscribe).toBeHidden();
    await expect(
      main
        .getByRole("table")
        .getByText(/Active/)
        .first(),
    ).toBeVisible();
    await expectCleanPage(page, issues, "card subscribed", { h1: cardName });
    await page.screenshot({
      path: "test-results/subscriber/card-page.png",
      fullPage: true,
    });

    // It is listed in the Ledger Subscribers tab and in Subscriptions.
    await visit(page, issues, `/folders/${demo.ledger.id}/cards`, {
      h1: "Ledger",
    });
    await expect(main.getByRole("link", { name: cardName })).toBeVisible();
    await visit(page, issues, "/cards", { h1: "Subscriptions" });
    await expect(main.getByRole("link", { name: cardName })).toBeVisible();

    // Archive it through the UI.
    await main.getByRole("link", { name: cardName }).click();
    await expect(
      page.getByRole("heading", { level: 1, name: cardName }),
    ).toBeVisible();
    issues.reset();
    await main.getByRole("button", { name: "Archive card" }).click();
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Archive card" })
      .click();
    await expect(page.getByText("This card is archived")).toBeVisible();
    await expectCleanPage(page, issues, "card archived", { h1: cardName });
  });
  // Cards live in folders of any kind: the Acme organization admin creates one at the Organization
  // itself (the Subscribers tab preselects it), subscribes it to a contract published below it and
  // finds it listed at the Organization.
  test("an organization admin creates and subscribes an organization-level card", async ({
    page,
  }) => {
    test.setTimeout(300_000);
    const issues = new IssueCollector(page);
    await signInAs(page, ACME_ORG_ADMIN);
    const main = page.getByRole("main");
    await visit(page, issues, `/folders/${demo.acme.id}/cards`, {
      h1: "Acme",
    });
    const cardName = `${CARD_PREFIX}org ${Date.now().toString(36)}`;
    await main.getByRole("button", { name: "Create card" }).first().click();
    const create = page.getByRole("dialog", { name: "Create card" });
    await expect(create.getByLabel("Folder")).toHaveValue(demo.acme.id);
    await create.getByLabel("Name").fill(cardName);
    await create
      .getByLabel("Description")
      .fill("An organization-level consumer (archived by the journey).");
    await create.getByRole("button", { name: "Create card" }).click();
    await expect(
      page.getByRole("heading", { level: 1, name: cardName }),
    ).toBeVisible();

    issues.reset();
    await page.getByRole("tab", { name: "Subscriptions" }).click();
    await main
      .getByRole("button", { name: "Subscribe to a contract" })
      .first()
      .click();
    const subscribe = page.getByRole("dialog", {
      name: "Subscribe to a contract",
    });
    await subscribe.getByLabel("Find a contract").fill(demo.orders.name);
    await subscribe
      .getByRole("list", { name: "Matching contracts" })
      .getByRole("button", { name: new RegExp(demo.orders.name) })
      .first()
      .click();
    await expect(subscribe.getByLabel("Version")).not.toHaveValue("");
    await subscribe.getByRole("button", { name: "Subscribe card" }).click();
    await expect(subscribe).toBeHidden();
    await expect(
      main
        .getByRole("table")
        .getByText(/Active/)
        .first(),
    ).toBeVisible();
    await expectCleanPage(page, issues, "organization card subscribed", {
      h1: cardName,
    });

    await visit(page, issues, `/folders/${demo.acme.id}/cards`, {
      h1: "Acme",
    });
    await expect(main.getByRole("link", { name: cardName })).toBeVisible();
  });
});
