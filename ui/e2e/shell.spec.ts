// The signed-in shell and folder explorer against the compose stack: navigation, creating an
// organization through the UI (per-run name), the folder page, and axe plus reflow checks.
import type { Page } from "@playwright/test";
import {
  expect,
  expectNoHorizontalOverflow,
  expectNoSeriousA11yViolations,
  test,
} from "./fixtures";
import { adminApi } from "./journey-helpers";

function adminPassword(): string {
  const password = process.env.DC_UI_E2E_ADMIN_PASSWORD;
  if (!password) {
    throw new Error("global-setup did not export DC_UI_E2E_ADMIN_PASSWORD");
  }
  return password;
}

async function signIn(page: Page) {
  await page.goto("/signin");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password", { exact: true }).fill(adminPassword());
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: /^Welcome, / }),
  ).toBeVisible();
}

const cleanupIds: string[] = [];

test.afterAll(async () => {
  if (cleanupIds.length === 0) return;
  const api = await adminApi();
  try {
    for (const id of cleanupIds.splice(0)) {
      await api.post(`identity/folders/${id}/archive`);
    }
  } finally {
    await api.dispose();
  }
});

test("the shell navigates and the folder explorer creates an organization", async ({
  page,
}) => {
  await signIn(page);
  const nav = page.getByRole("navigation", { name: "Primary" });
  await expect(nav.getByRole("link", { name: "Home" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expectNoSeriousA11yViolations(page);

  await nav.getByRole("link", { name: "Folders" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Folders" }),
  ).toBeVisible();
  await expect(page).toHaveTitle("Folders · Contractum");

  const name = `Shell e2e ${Date.now().toString(36)}`;
  await page
    .getByRole("button", { name: "Create organization", exact: true })
    // The page header's button; an empty ROOT also shows one in the empty state.
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "Create organization" });
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByRole("button", { name: "Create organization" }).click();

  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  const createdId = /\/folders\/([^/?#]+)/.exec(page.url())?.[1];
  // Archive the organization afterwards so repeated runs do not clutter the tree (never deleted).
  if (createdId) {
    cleanupIds.push(createdId);
  }
  await expect(
    page.getByRole("button", { name: "Create project" }),
  ).toBeVisible();
  // The folder tree lives on /folders only; folder pages offer the header picker instead.
  await expect(page.getByRole("treegrid")).toHaveCount(0);
  await expect(
    page.getByRole("combobox", { name: "Switch folder" }),
  ).toHaveValue(createdId ?? "");
  await expectNoSeriousA11yViolations(page);

  await page
    .getByRole("navigation", { name: "Folder sections" })
    .getByRole("link", { name: "Contracts" })
    .click();
  await expect(
    page.getByRole("heading", { name: "No contracts yet" }),
  ).toBeVisible();
});

test("the signed-in home reflows at 320 px", async ({ page }) => {
  await signIn(page);
  await page.setViewportSize({ width: 320, height: 800 });
  await expect(
    page.getByRole("button", { name: "Open navigation" }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
});
