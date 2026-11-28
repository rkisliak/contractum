// The whole UI against the real compose stack, signed in as the Global admin, with no mocks: every
// area and its main tabs render (h1, no ProblemAlert or error boundary, no console/page/API error,
// no CSP violation through the fixture, axe clean), then a run of real writes across areas.
// Demo data (Acme > Payments > Ledger/Fraud, the "Orders" contract, the acme-baseline rule set,
// the "Acme batch table" template, the demo-analytics-team card) is found through the API.
import type { Page } from "@playwright/test";
import { changeStatusToActiveAndSave, expect, test } from "./fixtures";
import {
  adminApi,
  cleanupJourneyTeams,
  expectCleanPage,
  findDemo,
  IssueCollector,
  signIn,
  visit,
  type Demo,
} from "./journey-helpers";

let demo: Demo;

// Fail a missing control in seconds, not at the 10-minute test timeout.
test.use({ actionTimeout: 15_000, navigationTimeout: 30_000 });

test.beforeAll(async () => {
  const api = await adminApi();
  try {
    demo = await findDemo(api);
    // Leftovers of an earlier run that failed before its own cleanup.
    await cleanupJourneyTeams(api, demo.payments.id);
  } finally {
    await api.dispose();
  }
});

// The journey's team, contract and card are archived or soft-deleted through the API afterwards,
// so repeated runs do not pile up "Journey ..." teams under Acme > Payments. afterAll runs even
// when a test failed or timed out; the demo is looked up again if beforeAll did not get that far.
test.afterAll(async () => {
  const api = await adminApi();
  try {
    const payments =
      (demo as Demo | undefined)?.payments.id ??
      (await findDemo(api)).payments.id;
    await cleanupJourneyTeams(api, payments);
  } finally {
    await api.dispose();
  }
});

async function clickTab(page: Page, name: string | RegExp) {
  await page.getByRole("tab", { name }).click();
}

test("every area renders cleanly against the real API", async ({ page }) => {
  test.setTimeout(600_000);
  const issues = new IssueCollector(page);
  await signIn(page);

  await visit(page, issues, "/", { h1: /^Welcome, / });

  // Folders
  await visit(page, issues, "/folders", { h1: "Folders" });
  await expect(
    page.getByRole("treegrid", { name: "All folders" }),
  ).toBeVisible();
  const f = `/folders/${demo.ledger.id}`;
  await visit(page, issues, f, { h1: "Ledger" });
  for (const seg of [
    "contracts",
    "templates",
    "rule-sets",
    "cards",
    "access",
  ]) {
    await visit(page, issues, `${f}/${seg}`, { h1: "Ledger" });
  }
  await visit(page, issues, `/folders/${demo.payments.id}/folders`, {
    h1: "Payments",
  });

  // Contracts
  await visit(page, issues, "/contracts", { h1: "Contracts" });
  await visit(page, issues, `/contracts/new?folder=${demo.ledger.id}`, {
    h1: "New contract",
  });
  const c = `/contracts/${demo.orders.id}`;
  await visit(page, issues, c, { h1: demo.orders.name });
  for (const seg of [
    "document",
    "drafts",
    "versions",
    "versions/1.0.0",
    "diff",
    "rule-sets",
    "subscribers",
  ]) {
    await visit(page, issues, `${c}/${seg}`, { h1: demo.orders.name });
  }
  await visit(page, issues, `${c}/diff?from=1.0.0&to=1.1.0`, {
    h1: demo.orders.name,
  });
  if (demo.draftId) {
    await visit(page, issues, `${c}/drafts/${demo.draftId}`, {
      h1: demo.orders.name,
    });
    await expect(page.locator(".monaco-editor").first()).toBeVisible();
  }

  // Templates
  await visit(page, issues, "/templates", { h1: "Templates" });
  await visit(page, issues, `/templates/${demo.template.id}`, {
    h1: demo.template.name,
  });
  await clickTab(page, "Versions");
  await expectCleanPage(page, issues, "template versions", {
    h1: demo.template.name,
  });

  // Rule sets and governance
  await visit(page, issues, "/rule-sets", { h1: "Rule sets" });
  await visit(page, issues, `/rule-sets?folder_id=${demo.acme.id}`, {
    h1: "Rule sets",
  });
  for (const tab of ["draft", "versions", "rollout", "impact"]) {
    await visit(page, issues, `/rule-sets/${demo.ruleSet.id}?tab=${tab}`, {
      h1: demo.ruleSet.name,
    });
  }
  await visit(page, issues, "/governance/dry-run", { h1: "Dry runs" });
  await visit(
    page,
    issues,
    `/governance/dry-run?rule_set_id=${demo.ruleSet.id}&version=1&folder_id=${demo.acme.id}`,
    { h1: "Dry runs" },
  );
  await visit(page, issues, "/governance/drift", { h1: "Drift" });
  await visit(page, issues, `/governance/drift?folder_id=${demo.acme.id}`, {
    h1: "Drift",
  });

  // Subscriptions
  await visit(page, issues, "/cards", { h1: "Subscriptions" });
  if (demo.card) {
    for (const tab of [
      "overview",
      "members",
      "subscriptions",
      "notices",
      "webhook",
    ]) {
      await visit(page, issues, `/cards/${demo.card.id}?tab=${tab}`, {
        h1: demo.card.name,
      });
    }
  }

  // Audit
  await visit(page, issues, "/audit", { h1: "Audit log" });
  const firstRow = page.getByRole("table").locator("tbody tr").first();
  await expect(firstRow).toBeVisible();
  issues.reset();
  await firstRow.getByRole("button").first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectCleanPage(page, issues, "audit drawer", {
    h1: "Audit log",
    dialog: true,
  });
  await page.keyboard.press("Escape");

  // Admin
  await visit(page, issues, "/admin/users", { h1: "Users" });
  await visit(page, issues, `/admin/users/${demo.userId}`, { h1: "admin" });
  await visit(page, issues, "/admin/groups", { h1: "Groups" });
  if (demo.groupId)
    await visit(page, issues, `/admin/groups/${demo.groupId}`, { h1: /.+/ });
  // Entra records are listed on the merged pages, filtered to Entra; the old tabbed URL only
  // redirects there.
  await visit(page, issues, "/admin/users?source=entra", { h1: "Users" });
  await visit(page, issues, "/admin/groups?source=entra", { h1: "Groups" });
  await visit(page, issues, "/admin/service-accounts?source=entra", {
    h1: "Service accounts",
  });
  await page.goto("/admin/entra?tab=groups");
  await expect(page).toHaveURL(/\/admin\/groups\?source=entra$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Groups");
  if (demo.entraUserId)
    await visit(page, issues, `/admin/entra/users/${demo.entraUserId}`, {
      h1: /.+/,
    });
  if (demo.entraGroupId)
    await visit(page, issues, `/admin/entra/groups/${demo.entraGroupId}`, {
      h1: /.+/,
    });
  if (demo.entraSpId)
    await visit(
      page,
      issues,
      `/admin/entra/service-principals/${demo.entraSpId}`,
      { h1: /.+/ },
    );
  await visit(page, issues, "/admin/service-accounts", {
    h1: "Service accounts",
  });
  if (demo.serviceAccountId)
    await visit(
      page,
      issues,
      `/admin/service-accounts/${demo.serviceAccountId}`,
      { h1: /.+/ },
    );
  await visit(page, issues, "/admin/pats", { h1: "All access tokens" });

  // Account
  await visit(page, issues, "/account/password", { h1: "Change password" });
  await visit(page, issues, "/account/tokens", { h1: "Access tokens" });
});

test("real writes across areas", async ({ page }) => {
  test.setTimeout(600_000);
  const issues = new IssueCollector(page);
  const run = Date.now().toString(36);
  await signIn(page);

  // 1. Create a Team folder under Acme > Payments.
  await visit(page, issues, `/folders/${demo.payments.id}`, { h1: "Payments" });
  const teamName = `Journey ${run}`;
  await page.getByRole("button", { name: "Create team" }).click();
  const createTeam = page.getByRole("dialog", { name: "Create team" });
  await createTeam.getByLabel("Name").fill(teamName);
  await createTeam.getByRole("button", { name: "Create team" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: teamName }),
  ).toBeVisible();
  const teamUrl = new URL(page.url()).pathname;
  await expectCleanPage(page, issues, "new team", { h1: teamName });

  // 2. A contract from the Acme template, through the folder's Templates tab.
  issues.reset();
  await page
    .getByRole("navigation", { name: "Folder sections" })
    .getByRole("link", { name: "Templates" })
    .click();
  await page
    .getByRole("link", { name: `Use in new contract: ${demo.template.name}` })
    .click();
  await expect(
    page.getByRole("heading", { level: 1, name: "New contract" }),
  ).toBeVisible();
  await expect(page.getByLabel("From a template")).toBeChecked();
  await expect(page.getByLabel("Template", { exact: true })).toHaveValue(
    demo.template.id,
  );
  const contractName = `Journey orders ${run}`;
  await page.getByLabel("Name").fill(contractName);
  await expectCleanPage(page, issues, "new contract from template", {
    h1: "New contract",
  });
  await page.getByRole("button", { name: "Create contract" }).click();
  await expect(page).toHaveURL(/\/contracts\/[^/]+\/drafts\/[^/]+$/);
  const contractId = new URL(page.url()).pathname.split("/")[2] ?? "";
  await expect(
    page.getByRole("heading", { level: 1, name: contractName }),
  ).toBeVisible();
  await expect(page.locator(".monaco-editor").first()).toBeVisible();

  // 3. Validate, then publish through the gate (acme-baseline is mandatory in warn mode).
  issues.reset();
  await page.getByRole("button", { name: "Validate draft" }).click();
  await expect(
    page.getByRole("heading", { name: "Validation findings" }),
  ).toBeVisible();
  await expectCleanPage(page, issues, "validated draft", { h1: contractName });
  // The template says `status: draft`: a released version needs `status: active` first.
  await changeStatusToActiveAndSave(page);
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  const publish = page.getByRole("dialog", { name: /^Publish .*\?$/ });
  await expect(publish).toBeVisible();
  await publish.getByRole("button", { name: /^Publish / }).click();
  await expect(page.getByText(/^Published .* as Latest$/)).toBeVisible();
  await expectCleanPage(page, issues, "published", { h1: contractName });
  const semver =
    (await page.getByText(/^Published .* as Latest$/).textContent())?.split(
      " ",
    )[1] ?? "";
  await visit(page, issues, `/contracts/${contractId}/versions/${semver}`, {
    h1: contractName,
  });

  // 4. A new draft with a minor bump, then the diff against Latest.
  await visit(page, issues, `/contracts/${contractId}/drafts`, {
    h1: contractName,
  });
  await page.getByRole("button", { name: "New draft" }).click();
  const newDraft = page.getByRole("dialog", { name: "Create a draft" });
  await newDraft.getByLabel("Version bump").selectOption("minor");
  await newDraft.getByRole("button", { name: "Create draft" }).click();
  await expect(page).toHaveURL(/\/drafts\/[^/]+$/);
  await expect(page.locator(".monaco-editor").first()).toBeVisible();
  await expectCleanPage(page, issues, "second draft", { h1: contractName });
  await page.getByRole("link", { name: "Diff" }).click();
  await expect(page).toHaveURL(/\/diff/);
  await expectCleanPage(page, issues, "diff", { h1: contractName });

  // 5. Grant a role on the new team to demo-alice.
  await visit(page, issues, `${teamUrl}/access`, { h1: teamName });
  await page.getByRole("button", { name: "Grant access" }).first().click();
  const grant = page.getByRole("dialog", { name: "Grant access" });
  await grant.getByLabel("Search users").fill("demo-alice");
  await grant.getByRole("listbox").getByRole("option").first().click();
  await grant.getByLabel("Role").selectOption({ index: 1 });
  await grant.getByRole("button", { name: "Grant access" }).click();
  await expect(grant).toBeHidden();
  await expect(
    page.getByRole("rowheader", { name: /demo-alice/ }).first(),
  ).toBeVisible();
  await expectCleanPage(page, issues, "granted", { h1: teamName });

  // 6. A personal access token: created, shown once, then revoked.
  await visit(page, issues, "/account/tokens", { h1: "Access tokens" });
  const tokenName = `journey-${run}`;
  await page.getByRole("button", { name: "Create token" }).click();
  const createToken = page.getByRole("dialog", { name: "Create token" });
  await createToken.getByLabel("Name").fill(tokenName);
  await createToken.getByRole("button", { name: "Create token" }).click();
  const reveal = page.getByRole("dialog", { name: /^Your new / });
  await expect(reveal).toBeVisible();
  await expect(reveal.getByText(/^dcp_/)).toBeVisible();
  await reveal.getByLabel(/^I have stored this /).check();
  await reveal.getByRole("button", { name: /^Close after storing / }).click();
  await expect(reveal).toBeHidden();
  const tokenRow = page.getByRole("row", { name: new RegExp(tokenName) });
  await tokenRow.getByRole("button", { name: /^Revoke/ }).click();
  const revoke = page.getByRole("alertdialog");
  const confirmText = revoke.getByRole("textbox");
  if (await confirmText.count()) await confirmText.fill(tokenName);
  await revoke.getByRole("button", { name: `Revoke ${tokenName}` }).click();
  await expect(revoke).toBeHidden();
  await expect(tokenRow.getByText("Revoked")).toBeVisible();
  await expectCleanPage(page, issues, "token revoked", { h1: "Access tokens" });

  // 7. A subscriber card in the new team, subscribed to the new contract.
  await visit(page, issues, `${teamUrl}/cards`, { h1: teamName });
  await page.getByRole("button", { name: "Create card" }).first().click();
  const createCard = page.getByRole("dialog", { name: "Create card" });
  const cardName = `journey-card-${run}`;
  await createCard.getByLabel("Name").fill(cardName);
  await createCard
    .getByLabel("Description")
    .fill("The journey test's consuming team.");
  await createCard.getByRole("button", { name: "Create card" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: cardName }),
  ).toBeVisible();
  issues.reset();
  await page.getByRole("tab", { name: "Subscriptions" }).click();
  await page.getByRole("button", { name: "Subscribe to a contract" }).click();
  const subscribe = page.getByRole("dialog", {
    name: "Subscribe to a contract",
  });
  await subscribe.getByLabel("Find a contract").fill(contractName);
  await subscribe
    .getByRole("list", { name: "Matching contracts" })
    .getByRole("button")
    .first()
    .click();
  await expect(subscribe.getByLabel("Version")).toHaveValue(semver);
  await subscribe.getByRole("button", { name: "Subscribe card" }).click();
  await expect(subscribe).toBeHidden();
  await expect(
    page.getByRole("link", { name: contractName }).first(),
  ).toBeVisible();
  await expectCleanPage(page, issues, "card subscribed", { h1: cardName });
  await visit(page, issues, `/contracts/${contractId}/subscribers`, {
    h1: contractName,
  });
  await expect(page.getByRole("link", { name: cardName })).toBeVisible();
});
