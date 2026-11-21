// UI-01: a local user changes their password through Account settings. The form submits, the
// session ends with the fixed "password-changed" notice on /signin, the new password signs in and
// the old one is refused with the generic message. A per-run user is created through the admin
// API so the admin and the demo personas never change password.
import { expect, test } from "./fixtures";
import { adminApi } from "./journey-helpers";
import { signInAs, type Persona } from "./personas";

test.use({ actionTimeout: 15_000, navigationTimeout: 30_000 });

const run = Date.now().toString(36);
const user: Persona = {
  username: `pw-change-${run}`,
  password: `Old-Pass-${run}-2026!`,
  displayName: `Pat Password ${run}`,
  folderPath: "",
  role: "",
};
const newPassword = `New-Pass-${run}-2026!`;

test.beforeAll(async () => {
  const api = await adminApi();
  try {
    await api.post("identity/users", {
      username: user.username,
      password: user.password,
      display_name: user.displayName,
    });
  } finally {
    await api.dispose();
  }
});

test("a local user changes their password and must sign in with the new one", async ({
  page,
}) => {
  await signInAs(page, user);

  await page.goto("/account/password");
  await expect(
    page.getByRole("heading", { level: 1, name: "Change password" }),
  ).toBeVisible();
  await page.getByLabel("Current password").fill(user.password);
  await page.getByLabel("New password", { exact: true }).fill(newPassword);
  await page.getByLabel("Confirm new password").fill(newPassword);
  await page.getByRole("button", { name: "Change password" }).click();

  await expect(page).toHaveURL(/\/signin/);
  await expect(
    page.getByText("Password changed. Sign in again."),
  ).toBeVisible();

  // The old password no longer works and gets the generic message.
  await page.getByLabel("Username").fill(user.username);
  await page.getByLabel("Password", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: `Welcome, ${user.displayName}`,
    }),
  ).toHaveCount(0);

  // The new one does.
  await signInAs(page, { ...user, password: newPassword });
});
