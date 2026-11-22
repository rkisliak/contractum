// Design screenshots (data-contracts-enterprise): home, /folders, a folder page and its contracts,
// /contracts, a contract and its versions, the rule set editor and the audit log, written to test-results/design/<page>-<project>.png in both the
// light (chromium) and dark (chromium-dark) projects for a visual review. Read-only. A second test
// shoots contract authoring (the New contract form in YAML and JSON, the draft editor).
import { expect, test } from "./fixtures";
import { adminApi, signIn } from "./journey-helpers";

// The clipboard permission must exist when the browser context is created: granting it later on
// the running context no longer reaches the page in the Chromium of Playwright 1.65, so Monaco
// never received the pasted text.
test.use({ permissions: ["clipboard-read", "clipboard-write"] });

type Page_<T> = { items: T[] };
type Folder = { id: string; path: string; kind: string };

test("design screenshots of the main screens", async ({ page }, info) => {
  test.setTimeout(180_000);
  const api = await adminApi();
  let ledger: Folder | undefined;
  let orders: { id: string } | undefined;
  let ruleSet: { id: string } | undefined;
  try {
    const folders = await api.get<Page_<Folder>>("identity/folders?limit=100");
    ledger = folders.items.find((f) => f.path === "acme/payments/ledger");
    const acme = folders.items.find((f) => f.path === "acme");
    if (ledger) {
      const contracts = await api.get<Page_<{ id: string }>>(
        `contracts/contracts?folder_id=${ledger.id}&odcs_id=urn:acme:ledger:orders`,
      );
      orders = contracts.items[0];
    }
    if (acme) {
      const sets = await api.get<Page_<{ id: string; name: string }>>(
        `validation/folders/${acme.id}/rule-sets`,
      );
      ruleSet = sets.items.find((r) => r.name === "acme-baseline");
    }
  } finally {
    await api.dispose();
  }

  await page.setViewportSize({ width: 1440, height: 960 });
  await signIn(page);
  const shots: [string, string | null][] = [
    ["home", "/"],
    ["folders", "/folders"],
    ["folder", ledger ? `/folders/${ledger.id}` : null],
    ["folder-contracts", ledger ? `/folders/${ledger.id}/contracts` : null],
    ["contracts", "/contracts"],
    ["contract", orders ? `/contracts/${orders.id}` : null],
    ["contract-versions", orders ? `/contracts/${orders.id}/versions` : null],
    ["rule-set-editor", ruleSet ? `/rule-sets/${ruleSet.id}?tab=draft` : null],
    ["audit", "/audit"],
  ];
  for (const [name, path] of shots) {
    if (path === null) continue;
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible({
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle").catch(() => undefined);
    await expect(page.locator("[aria-busy='true']"))
      .toHaveCount(0, { timeout: 10_000 })
      .catch(() => undefined);
    await page.screenshot({
      path: `test-results/design/${name}-${info.project.name}.png`,
      fullPage: true,
    });
  }
});

// Contract authoring: the "New contract" form with a realistic ODCS document pasted into the YAML
// editor (never submitted), the same form in JSON, and the draft editor after "Validate draft".
const SAMPLE_YAML = `apiVersion: v3.1.0
kind: DataContract
id: urn:acme:payments:ledger:settlements
name: Settlements
version: 0.1.0
status: draft
domain: payments
description:
  purpose: Daily card settlements per merchant, used by finance reconciliation.
team:
  name: ledger
schema:
  - name: settlements
    physicalType: table
    description: One row per settled card transaction.
    properties:
      - name: settlement_id
        logicalType: string
        required: true
        primaryKey: true
      - name: merchant_id
        logicalType: string
        required: true
      - name: amount
        logicalType: number
        required: true
      - name: currency
        logicalType: string
        description: ISO 4217 code, for example EUR.
      - name: settled_on
        logicalType: date
`;

const SAMPLE_JSON = {
  apiVersion: "v3.1.0",
  kind: "DataContract",
  id: "urn:acme:payments:ledger:settlements",
  name: "Settlements",
  version: "0.1.0",
  status: "draft",
  domain: "payments",
  description: {
    purpose:
      "Daily card settlements per merchant, used by finance reconciliation.",
  },
  team: { name: "ledger" },
  schema: [
    {
      name: "settlements",
      physicalType: "table",
      properties: [
        { name: "settlement_id", logicalType: "string", required: true },
        { name: "amount", logicalType: "number", required: true },
      ],
    },
  ],
};

test("design screenshots of contract authoring", async ({ page }, info) => {
  test.setTimeout(180_000);
  const api = await adminApi();
  let ledger: Folder | undefined;
  let orders: { id: string } | undefined;
  let draftId: string | undefined;
  try {
    const folders = await api.get<Page_<Folder>>("identity/folders?limit=100");
    ledger = folders.items.find((f) => f.path === "acme/payments/ledger");
    if (ledger) {
      const contracts = await api.get<Page_<{ id: string }>>(
        `contracts/contracts?folder_id=${ledger.id}&odcs_id=urn:acme:ledger:orders`,
      );
      orders = contracts.items[0];
    }
    if (orders) {
      const drafts = await api.get<Page_<{ id: string }>>(
        `contracts/contracts/${orders.id}/drafts`,
      );
      draftId = drafts.items[0]?.id;
    }
  } finally {
    await api.dispose();
  }
  test.skip(
    ledger === undefined,
    "demo folder acme/payments/ledger is missing",
  );

  await page.setViewportSize({ width: 1440, height: 960 });
  await signIn(page);
  const shot = (name: string) =>
    page.screenshot({
      path: `test-results/design/${name}-${info.project.name}.png`,
      fullPage: true,
    });

  await page.goto(`/contracts/new?folder=${ledger?.id ?? ""}`);
  await expect(
    page.getByRole("heading", { level: 1, name: "New contract" }),
  ).toBeVisible();
  await page.getByLabel("Blank document").check();
  const editor = page.locator(".monaco-editor").first();
  await expect(editor).toBeVisible({ timeout: 15_000 });
  await page.evaluate(
    (text) => navigator.clipboard.writeText(text),
    SAMPLE_YAML,
  );
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.press("ControlOrMeta+V");
  await page.keyboard.press("ControlOrMeta+Home");
  // Text only the pasted sample has: the blank template also contains "apiVersion".
  await expect(editor).toContainText("urn:acme:payments:ledger:settlements");
  await page.mouse.click(5, 5);
  await shot("new-contract-yaml");

  await page.getByLabel("Format").selectOption("json");
  await expect(editor).toContainText('"apiVersion"');
  await page.evaluate(
    (text) => navigator.clipboard.writeText(text),
    `${JSON.stringify(SAMPLE_JSON, null, 2)}\n`,
  );
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.press("ControlOrMeta+V");
  await page.keyboard.press("ControlOrMeta+Home");
  await expect(editor).toContainText("Settlements");
  await page.mouse.click(5, 5);
  await shot("new-contract-json");

  if (orders && draftId) {
    await page.goto(`/contracts/${orders.id}/drafts/${draftId}`);
    await expect(page.locator(".monaco-editor").first()).toBeVisible({
      timeout: 15_000,
    });
    const validate = page.getByRole("button", { name: "Validate draft" });
    if (await validate.isEnabled()) {
      await validate.click();
      await page.waitForLoadState("networkidle").catch(() => undefined);
      await expect(page.locator("[aria-busy='true']"))
        .toHaveCount(0, { timeout: 15_000 })
        .catch(() => undefined);
    }
    await shot("draft-editor");
  }
});
