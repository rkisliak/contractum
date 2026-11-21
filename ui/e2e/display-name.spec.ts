// Quick 261119-16f: display names. A local user renames themself on Account settings -> Profile
// and the new name shows on Home, in the account menu and on the admin user page; the Global admin
// renames the same user with "Edit name"; a non-admin persona is never offered "Edit name". A
// per-run user is created through the admin API so the admin and the demo personas keep their names.
import { expect, test } from "./fixtures";
import { adminApi, signIn } from "./journey-helpers";
import {
  ensurePersonas,
  LEDGER_EDITOR,
  openAccountMenu,
  signInAs,
  type Persona,
} from "./personas";

test.use({ actionTimeout: 15_000, navigationTimeout: 30_000 });
test.describe.configure({ mode: "serial" });

const run = Date.now().toString(36);
const user: Persona = {
  username: `rename-${run}`,
  password: `Rename-Pass-${run}-2026!`,
  displayName: `Rena Before ${run}`,
  folderPath: "",
  role: "",
};
const selfName = `Rena Self ${run}`;
const adminName = `Rena Admin ${run}`;
let userId = "";

test.beforeAll(async () => {
  const api = await adminApi();
  try {
    const created = await api.post<{ id: string }>("identity/users", {
      username: user.username,
      password: user.password,
      display_name: user.displayName,
    });
    userId = created.id;
    await ensurePersonas(api, [LEDGER_EDITOR]);
  } finally {
    await api.dispose();
  }
});

test("a local user renames themself on Account settings and sees it everywhere", async ({
  page,
}) => {
  await signInAs(page, user);

  await page.goto("/account/profile");
  await expect(
    page.getByRole("heading", { level: 1, name: "Profile" }),
  ).toBeVisible();
  const field = page.getByLabel("Display name");
  await expect(field).toHaveValue(user.displayName);
  await field.fill(`  ${selfName}  `);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Display name saved.").first()).toBeVisible();
  await expect(field).toHaveValue(selfName);

  // Home and the account menu read /me, which the save refreshed (no reload).
  await page
    .getByRole("link", { name: /^Home$/ })
    .first()
    .click();
  await expect(
    page.getByRole("heading", { level: 1, name: `Welcome, ${selfName}` }),
  ).toBeVisible();
  await openAccountMenu(page);
  await expect(
    page.getByRole("menu").getByText(selfName, { exact: true }),
  ).toBeVisible();
});

test("the Global admin renames a user from Admin -> Users", async ({
  page,
}) => {
  await signIn(page);
  await page.goto(`/admin/users/${userId}`);
  await expect(
    page.getByRole("heading", { level: 1, name: selfName }),
  ).toBeVisible();

  await page.getByRole("button", { name: "Edit name" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit display name" });
  await dialog.getByLabel("Display name").fill(adminName);
  await dialog.getByRole("button", { name: "Save display name" }).click();
  await expect(dialog).toBeHidden();
  await expect(
    page.getByRole("heading", { level: 1, name: adminName }),
  ).toBeVisible();
});

test("a non-admin persona is never offered Edit name", async ({ page }) => {
  await signInAs(page, LEDGER_EDITOR);
  await page.goto(`/admin/users/${userId}`);
  // The editor holds no user:read: the page settles on the API's refusal, never an editable name.
  await expect(
    page
      .getByRole("heading", { level: 1 })
      .or(page.getByRole("main").getByRole("alert"))
      .first(),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit name" })).toHaveCount(0);

  // The editor's own Profile page offers the self-service edit only.
  await page.goto("/account/profile");
  await expect(page.getByLabel("Display name")).toHaveValue(
    LEDGER_EDITOR.displayName,
  );
  await expect(page.getByRole("button", { name: "Save" })).toBeVisible();
});
