// The Folders tree with contracts (TREE-03, TREE-05, D-18) walked in a real browser against the
// real stack, as the Global admin and as three non-admin personas: a Team reader, a Consumer and a
// Policy Manager. Each persona ticks "Show contracts in tree", expands Acme, Payments and Ledger
// (the folders it can see), and the contract rows rendered under Ledger must equal, name for name
// and in the same order, the items of the list response that persona's browser received for the
// Ledger folder (include_subtree=false, sort=name). Each row shows its lifecycle badge and version
// right after the name. A row opens its contract by click, by Enter and by its open action; the
// Policy Manager lands on the metadata-only page. Expand all and Collapse all are disabled whenever
// they would change nothing. No console error, page error or API error of
// any status is allowed (nothing is allow-listed) and the tree passes the serious axe gate.
import type { Locator, Page, Response } from "@playwright/test";
import { expect, expectNoSeriousA11yViolations, test } from "./fixtures";
import {
  adminApi,
  expectCleanPage,
  findDemo,
  IssueCollector,
  signIn,
  type Demo,
} from "./journey-helpers";
import {
  ACME_POLICY_MANAGER,
  ensurePersonas,
  LEDGER_READER,
  LEDGER_SUBSCRIBER,
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

type ListItem = { id: string; name: string | null; odcs_id: string };
type ListBody = { items: ListItem[]; next_cursor: string | null };

const METADATA_ONLY =
  "You can see this contract's metadata only. Its content and versions are not available at your access level.";

function grid(page: Page): Locator {
  return page.getByRole("treegrid", { name: "All folders" });
}

/** A folder row by its id: row ids end in `-f_<folder id>` (the tree's DOM id scheme). */
function folderRow(page: Page, folderId: string): Locator {
  return grid(page).locator(`[role="row"][id$="-f_${folderId}"]`);
}

function contractRow(page: Page, contractId: string): Locator {
  return grid(page).locator(`[role="row"][id$="-c_${contractId}"]`);
}

/** The response to the tree's list call for one folder (what the persona's browser received). */
function treeResponse(page: Page, folderId: string): Promise<Response> {
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname.endsWith("/api/v1/contracts/contracts") &&
      url.searchParams.get("folder_id") === folderId &&
      url.searchParams.get("include_subtree") === "false" &&
      url.searchParams.get("sort") === "name" &&
      response.request().method() === "GET"
    );
  });
}

/**
 * Expands every row of the Acme > Payments > Ledger path the persona can see, by clicking the row
 * or, with `keyboard`, by focusing it and pressing ArrowRight. With `waitForLedger` it returns the
 * Ledger tree response when this call opened Ledger, else null (already open).
 */
async function expandPath(
  page: Page,
  keyboard: boolean,
  waitForLedger = true,
): Promise<Response | null> {
  let ledger: Promise<Response> | null = null;
  for (const folder of [demo.acme, demo.payments, demo.ledger]) {
    const row = folderRow(page, folder.id);
    if ((await row.count()) === 0) continue; // a parent this persona cannot see is not a row
    if ((await row.getAttribute("aria-expanded")) === "true") continue;
    if (waitForLedger && folder.id === demo.ledger.id)
      ledger = treeResponse(page, folder.id);
    if (keyboard) {
      await row.focus();
      await page.keyboard.press("ArrowRight");
    } else {
      await row.click();
    }
    await expect(row).toHaveAttribute("aria-expanded", "true");
  }
  return ledger === null ? null : await ledger;
}

/** The names of the contract rows rendered under the Ledger folder row, in DOM order. */
async function renderedLedgerNames(page: Page): Promise<string[]> {
  return grid(page).evaluate((root, ledgerId) => {
    const rows = [
      ...root.querySelectorAll<HTMLElement>('[role="row"][aria-level]'),
    ];
    const start = rows.findIndex((row) => row.id.endsWith(`-f_${ledgerId}`));
    if (start < 0) return [];
    const level = Number(rows[start]?.getAttribute("aria-level"));
    const names: string[] = [];
    for (const row of rows.slice(start + 1)) {
      if (Number(row.getAttribute("aria-level")) <= level) break;
      if (row.id.includes("-c_"))
        names.push(row.querySelector("[title]")?.getAttribute("title") ?? "");
    }
    return names;
  }, demo.ledger.id);
}

async function openTree(page: Page): Promise<void> {
  await page.goto("/folders");
  await expect(
    page.getByRole("heading", { level: 1, name: "Folders" }),
  ).toBeVisible();
  await expect(grid(page)).toBeVisible();
}

function treeButton(page: Page, name: "Expand all" | "Collapse all"): Locator {
  return page
    .getByRole("group", { name: "Tree actions" })
    .getByRole("button", { name });
}

async function walk(
  page: Page,
  signInFn: () => Promise<void>,
  options: { keyboard: boolean; metadataOnly: boolean },
): Promise<void> {
  const issues = new IssueCollector(page);
  await signInFn();
  issues.reset();

  await openTree(page);
  // Only Root is open (or nothing is, for a persona without Root): nothing to collapse yet.
  await expect(treeButton(page, "Collapse all")).toBeDisabled();
  await page.getByLabel("Show contracts in tree").check();
  await expect(page).toHaveURL(/[?&]contracts=true/);

  const response = await expandPath(page, options.keyboard);
  if (response === null) throw new Error("Ledger was already open");
  expect(response.status()).toBe(200);
  const body = (await response.json()) as ListBody;
  const expected = body.items.map((item) => item.name || item.odcs_id);
  expect(expected, "the persona's Ledger list holds Orders").toContain(
    demo.orders.name,
  );

  // The rendered rows are exactly the response's items, in the same order, with no overflow row
  // (the Ledger holds far fewer than one page).
  await expect
    .poll(() => renderedLedgerNames(page), "contract rows under Ledger")
    .toEqual(expected);
  expect(body.next_cursor, "Ledger fits one page").toBeNull();
  await expect(page.locator("[aria-busy='true']")).toHaveCount(0);
  const orders = body.items.find((item) => item.id === demo.orders.id);
  expect(orders, "Orders is in the Ledger response").toBeDefined();
  await expect(contractRow(page, demo.orders.id)).toBeVisible();
  // The badge and version follow the name ("Latest 1.1.0"), never a bare number in a count column.
  const meta = contractRow(page, demo.orders.id).locator(
    "[data-contract-meta]",
  );
  await expect(meta).toBeVisible();
  await expect(meta).toHaveText(
    /^(Latest|Preview|Draft|Unpublished).*(\d+\.\d+\.\d+|Not published|No Latest version)$/,
  );
  const openAction = contractRow(page, demo.orders.id).getByRole("link", {
    name: `Open ${demo.orders.name}`,
  });
  await expect(openAction).toBeVisible();

  await expectNoSeriousA11yViolations(page);
  expect(issues.take(), "console, page or API errors on the tree").toEqual([]);

  // Open Orders by clicking its row (the cell beside the name, not the link).
  const contractUrl = new RegExp(`/contracts/${demo.orders.id}$`);
  await contractRow(page, demo.orders.id).getByRole("gridcell").first().click();
  await expect(page).toHaveURL(contractUrl);
  await expectOrders(page, issues, options.metadataOnly);

  // Back to the tree (the toggle stays in the URL) and open it again with Enter on the row.
  await page.goBack();
  await expect(page).toHaveURL(/[?&]contracts=true/);
  await expect(grid(page)).toBeVisible();
  // The Ledger list is still cached (30 s), so reopening it makes no new call: do not wait for one.
  await expandPath(page, options.keyboard, false);
  const row = contractRow(page, demo.orders.id);
  await expect(row).toBeVisible();
  await row.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(contractUrl);
  await expectOrders(page, issues, options.metadataOnly);

  // Once more through the row's open action, reached with Tab from the focused row.
  await page.goBack();
  await expect(grid(page)).toBeVisible();
  await expandPath(page, options.keyboard, false);
  await contractRow(page, demo.orders.id).focus();
  await page.keyboard.press("Tab");
  await expect(
    contractRow(page, demo.orders.id).getByRole("link", {
      name: `Open ${demo.orders.name}`,
    }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(contractUrl);
  await expectOrders(page, issues, options.metadataOnly);

  // Expand all opens everything, then has nothing left to do; Collapse all likewise.
  await page.goBack();
  await expect(grid(page)).toBeVisible();
  await treeButton(page, "Expand all").click();
  await expect(treeButton(page, "Expand all")).toBeDisabled();
  await expect(treeButton(page, "Collapse all")).toBeEnabled();
  await treeButton(page, "Collapse all").click();
  await expect(treeButton(page, "Collapse all")).toBeDisabled();
  await expect(treeButton(page, "Expand all")).toBeEnabled();
  expect(issues.take(), "console, page or API errors on the tree").toEqual([]);
}

async function expectOrders(
  page: Page,
  issues: IssueCollector,
  metadataOnly: boolean,
): Promise<void> {
  await expectCleanPage(page, issues, "Orders from the tree", {
    h1: demo.orders.name,
  });
  const sections = page.getByRole("navigation", { name: "Contract sections" });
  if (metadataOnly) {
    // The Policy Manager reads metadata only: the notice, and no tab that would be refused.
    await expect(page.getByText(METADATA_ONLY)).toBeVisible();
    await expect(
      sections.getByRole("link", { name: "Overview" }),
    ).toBeVisible();
    for (const name of ["Versions", "Diff", "Document", "Drafts"])
      await expect(
        sections.getByRole("link", { name, exact: true }),
      ).toHaveCount(0);
  } else {
    await expect(page.getByText(METADATA_ONLY)).toHaveCount(0);
  }
}

test("the Global admin walks the tree with contracts", async ({ page }) => {
  test.setTimeout(240_000);
  await walk(page, () => signIn(page), {
    keyboard: false,
    metadataOnly: false,
  });
});

const PERSONA_WALKS: {
  persona: Persona;
  label: string;
  keyboard: boolean;
  metadataOnly: boolean;
}[] = [
  // The Team reader expands by keyboard (focus a row, ArrowRight).
  {
    persona: LEDGER_READER,
    label: "a Team reader",
    keyboard: true,
    metadataOnly: false,
  },
  {
    persona: LEDGER_SUBSCRIBER,
    label: "a Consumer",
    keyboard: false,
    metadataOnly: false,
  },
  {
    persona: ACME_POLICY_MANAGER,
    label: "a Policy Manager",
    keyboard: false,
    metadataOnly: true,
  },
];

for (const { persona, label, keyboard, metadataOnly } of PERSONA_WALKS) {
  test(`${label} (${persona.username}) walks the tree with contracts`, async ({
    page,
  }) => {
    test.setTimeout(240_000);
    await walk(page, () => signInAs(page, persona), { keyboard, metadataOnly });
  });
}
