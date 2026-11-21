// Deleting a folder through the UI against the real stack (quick 261119-0v1): an admin creates
// a uniquely named empty team under Acme > Payments through the API, opens it, deletes it from the
// folder actions menu through the confirm dialog and lands on Payments with the team gone. On
// Payments (which holds subfolders) the same menu item is aria-disabled but focusable and its
// tooltip names the subfolders. Leftover "Delete journey ..." teams of an earlier run are removed
// before and after.
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import {
  adminApi,
  expectCleanPage,
  findDemo,
  IssueCollector,
  signIn,
  type Api,
  type Demo,
} from "./journey-helpers";

test.use({ actionTimeout: 15_000, navigationTimeout: 30_000 });

const PREFIX = "Delete journey ";
let demo: Demo;

type Folder = { id: string; display_name: string; parent_id: string | null };

async function deleteLeftovers(api: Api): Promise<void> {
  const folders = await api.get<{ items: Folder[] }>(
    "identity/folders?limit=100&include_archived=true",
  );
  for (const folder of folders.items)
    if (
      folder.parent_id === demo.payments.id &&
      folder.display_name.startsWith(PREFIX)
    )
      await api.delete(`identity/folders/${folder.id}`);
}

test.beforeAll(async () => {
  const api = await adminApi();
  try {
    demo = await findDemo(api);
    await deleteLeftovers(api);
  } finally {
    await api.dispose();
  }
});

test.afterAll(async () => {
  const api = await adminApi();
  try {
    await deleteLeftovers(api);
  } finally {
    await api.dispose();
  }
});

async function openActions(page: Page, name: string): Promise<void> {
  await page.getByRole("button", { name: `Actions for ${name}` }).click();
}

test("an admin deletes an empty team and lands on its parent", async ({
  page,
}) => {
  const name = `${PREFIX}${Date.now().toString(36)}`;
  const api = await adminApi();
  let teamId: string;
  try {
    const created = await api.post<{ id: string }>("identity/folders", {
      parent_id: demo.payments.id,
      slug: `delete-journey-${Date.now().toString(36)}`,
      display_name: name,
    });
    teamId = created.id;
  } finally {
    await api.dispose();
  }
  const issues = new IssueCollector(page);
  await signIn(page);
  await page.goto(`/folders/${teamId}`);
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();

  await openActions(page, name);
  const item = page.getByRole("menuitem", { name: "Delete folder…" });
  await expect(item).toBeVisible();
  await expect(item).not.toHaveAttribute("aria-disabled", "true");
  await item.click();

  const dialog = page.getByRole("alertdialog", { name: `Delete ${name}?` });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("This cannot be undone");
  await expectCleanPage(page, issues, "delete dialog", {
    h1: name,
    dialog: true,
  });
  await dialog.getByRole("button", { name: `Delete ${name}` }).click();

  await expect(page).toHaveURL(new RegExp(`/folders/${demo.payments.id}$`));
  await expect(
    page.getByRole("heading", { level: 1, name: "Payments" }),
  ).toBeVisible();
  await expect(page.getByText(`Deleted ${name}.`).first()).toBeVisible();
  await page.goto("/folders");
  await expect(page.getByText(name)).toHaveCount(0);
});

test("a folder that still holds subfolders keeps Delete disabled with the reason", async ({
  page,
}) => {
  await signIn(page);
  await page.goto(`/folders/${demo.payments.id}`);
  await expect(
    page.getByRole("heading", { level: 1, name: "Payments" }),
  ).toBeVisible();
  await openActions(page, "Payments");
  const item = page.getByRole("menuitem", { name: /Delete folder…/ });
  await expect(item).toHaveAttribute("aria-disabled", "true");
  await item.focus();
  await expect(item).toBeFocused();
  await expect(
    page.getByRole("tooltip").getByText(/contains: .*subfolder/),
  ).toBeVisible();
  // Selecting an unavailable item does nothing and keeps the menu open (Menu unavailableReason).
  await page.keyboard.press("Enter");
  await expect(item).toBeVisible();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
});
