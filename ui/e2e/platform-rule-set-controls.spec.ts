// Pause, Resume and Change date... on a platform rule set (quick 261118-vpa), as the Global admin
// and as personas who must not see them. The admin run never leaves the stack paused and never
// writes a new date: the live stack's publish gate is shared with every other journey.
import { expect, test } from "./fixtures";
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
  signInAs,
  type Persona,
} from "./personas";

/** odcs-consistency: seeded by migration 0006, mandatory, platform scope. */
const CONSISTENCY_ID = "841826b2-2418-480d-b34b-b600f504931d";
const PAGE = `/rule-sets/${CONSISTENCY_ID}?tab=rollout`;

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

/** Makes sure the shared stack is not left paused, whatever the test did. */
async function forceResume(): Promise<void> {
  const api = await adminApi();
  try {
    await api.put(
      `validation/folders/${demo.root.id}/platform-rule-sets/${CONSISTENCY_ID}/paused`,
      { paused: false },
    );
  } finally {
    await api.dispose();
  }
}

test("a Global admin pauses and resumes a platform rule set and sees the date dialog", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const issues = new IssueCollector(page);
  try {
    await signIn(page);
    // A platform set has no folder-owned record: the page probes it (404) and falls back to ROOT's
    // available-rule-sets view, as for the personas below.
    issues.reset([404]);
    await page.goto(PAGE);
    await expect(
      page.getByRole("heading", { level: 1, name: "odcs-consistency" }),
    ).toBeVisible();
    const main = page.getByRole("main");
    await expect(main.getByRole("button", { name: "Pause" })).toBeVisible();

    await main.getByRole("button", { name: "Pause" }).click();
    const pause = page.getByRole("alertdialog", {
      name: "Pause odcs-consistency?",
    });
    await expect(pause).toContainText("won't block publishing");
    await pause.getByRole("button", { name: "Pause odcs-consistency" }).click();
    await expect(pause).toBeHidden();
    await expect(main.getByRole("button", { name: "Resume" })).toBeVisible();
    await expect(
      main.getByText("Paused", { exact: true }).first(),
    ).toBeVisible();

    await main.getByRole("button", { name: "Resume" }).click();
    const resume = page.getByRole("alertdialog", {
      name: "Resume odcs-consistency?",
    });
    await resume
      .getByRole("button", { name: "Resume odcs-consistency" })
      .click();
    await expect(resume).toBeHidden();
    await expect(main.getByRole("button", { name: "Pause" })).toBeVisible();

    // The date dialog opens with a date field; dismissing it writes nothing.
    await main
      .getByRole("button", { name: /Change date/ })
      .first()
      .click();
    const dates = page.getByRole("alertdialog", {
      name: /Change the enforcement date of version 1/,
    });
    await expect(dates.getByLabel("Blocks publishes from (UTC)")).toBeVisible();
    await dates.getByRole("button", { name: "Go back" }).click();
    await expect(dates).toBeHidden();

    await expectCleanPage(page, issues, PAGE, { h1: "odcs-consistency" });
  } finally {
    await forceResume();
  }
});

async function sees(
  page: import("@playwright/test").Page,
  persona: Persona,
  folderId: string,
): Promise<void> {
  const issues = new IssueCollector(page);
  await signInAs(page, persona);
  issues.reset([403, 404]);
  await page.goto(`/rule-sets/${CONSISTENCY_ID}?via=${folderId}&tab=rollout`);
  await expect(
    page.getByRole("heading", { level: 1, name: "odcs-consistency" }),
  ).toBeVisible();
  const main = page.getByRole("main");
  await expect(
    main.getByRole("table", { name: /Rollout schedule/ }),
  ).toBeVisible();
  for (const name of [/^Pause/, /^Resume/, /Change date/, /Enforce version/])
    await expect(main.getByRole("button", { name })).toHaveCount(0);
}

test("a Policy Manager below ROOT sees no pause or date controls", async ({
  page,
}) => {
  await sees(page, ACME_POLICY_MANAGER, demo.acme.id);
});

test("a Team reader sees no pause or date controls", async ({ page }) => {
  await sees(page, LEDGER_READER, demo.ledger.id);
});
