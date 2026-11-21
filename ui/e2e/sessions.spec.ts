// Quick 261120-s0p: the Sessions card (ASVS 7.4.5 and 7.5.2). A non-admin persona with two
// browser sessions lists them on Account settings -> Profile, runs "Sign out everywhere else" and
// the other browser lands on /signin at its next refresh. The Global admin signs a user in once
// through the API, lists that user's sessions on the user detail page and signs them all out. A
// non-admin sees no admin-only control. A dedicated persona keeps other specs undisturbed.
import { request, type Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import {
  adminApi,
  expectCleanPage,
  IssueCollector,
  signIn,
  type Api,
} from "./journey-helpers";
import { ensurePersonas, SESSIONS_USER, signInAs } from "./personas";

test.use({ actionTimeout: 15_000, navigationTimeout: 30_000 });
test.describe.configure({ mode: "serial" });

const GATEWAY_URL = process.env.DC_UI_E2E_SETUP_URL ?? "https://traefik";

let userId = "";
let adminUserId = "";

async function userIdOf(api: Api, username: string): Promise<string> {
  const found = await api.get<{ items: { id: string }[] }>(
    `identity/users?username=${username}`,
  );
  const id = found.items[0]?.id;
  if (!id) throw new Error(`user ${username} missing`);
  return id;
}

/** Ends every session of the persona (what an earlier test or run left behind). */
async function resetSessions(): Promise<void> {
  const api = await adminApi();
  try {
    await api.post(`identity/users/${userId}/sessions/revoke-all`);
  } finally {
    await api.dispose();
  }
}

test.beforeAll(async () => {
  const api = await adminApi();
  try {
    await ensurePersonas(api, [SESSIONS_USER]);
    userId = await userIdOf(api, SESSIONS_USER.username);
    adminUserId = await userIdOf(api, "admin");
  } finally {
    await api.dispose();
  }
  await resetSessions();
});

/** Signs the persona in through the API only (no browser), as a pipeline would. */
async function apiSignIn(): Promise<void> {
  const ctx = await request.newContext({
    baseURL: GATEWAY_URL,
    ignoreHTTPSErrors: true,
  });
  try {
    const res = await ctx.post("/api/v1/identity/auth/login", {
      data: {
        username: SESSIONS_USER.username,
        password: SESSIONS_USER.password,
      },
    });
    expect(res.ok(), "API sign-in of the persona").toBe(true);
  } finally {
    await ctx.dispose();
  }
}

async function openProfile(page: Page): Promise<void> {
  await page.goto("/account/profile");
  await expect(
    page.getByRole("heading", { level: 1, name: "Profile" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { level: 2, name: "Sessions" }),
  ).toBeVisible();
}

test("a non-admin lists two sessions and signs out everywhere else", async ({
  page,
  browser,
  baseURL,
}) => {
  const issues = new IssueCollector(page);
  await signInAs(page, SESSIONS_USER);

  // A second browser: its own cookie jar, so its own session.
  const other = await browser.newContext({
    ...(baseURL ? { baseURL } : {}),
    ignoreHTTPSErrors: true,
  });
  const otherPage = await other.newPage();
  try {
    await signInAs(otherPage, SESSIONS_USER);

    issues.reset();
    await openProfile(page);
    const table = page.getByRole("table", { name: "Your active sessions" });
    await expect(table.getByRole("row")).toHaveCount(3); // header + two sessions
    await expect(table.getByText("This session")).toHaveCount(1);
    await expect(
      page.getByText(
        "2 of at most 100 sessions. Signing in beyond that ends the oldest one.",
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Sign out everywhere else" }),
    ).toBeVisible();

    await page
      .getByRole("button", { name: "Sign out everywhere else" })
      .click();
    const dialog = page.getByRole("alertdialog", {
      name: "Sign out every other session?",
    });
    await expect(dialog).toBeVisible();
    await dialog
      .getByRole("button", { name: "Sign out everywhere else" })
      .click();
    await expect(
      page.getByText("Signed out 1 other session.").first(),
    ).toBeVisible();
    await expect(table.getByRole("row")).toHaveCount(2); // header + this session
    await expect(
      page.getByRole("button", { name: "Sign out everywhere else" }),
    ).toHaveCount(0);
    await expectCleanPage(page, issues, "profile after signing out others", {
      h1: "Profile",
    });

    // The other browser keeps working until its access token needs a refresh, then lands on the
    // sign-in page: a reload runs the refresh at once.
    await otherPage.reload();
    await expect(otherPage).toHaveURL(/\/signin/);
    await expect(
      otherPage.getByRole("button", { name: "Sign in", exact: true }),
    ).toBeVisible();
  } finally {
    await other.close();
  }
});

test("the Global admin lists a user's sessions and signs them all out", async ({
  page,
}) => {
  await resetSessions();
  await apiSignIn();
  const issues = new IssueCollector(page);
  await signIn(page);
  issues.reset();
  await page.goto(`/admin/users/${userId}`);
  await expect(
    page.getByRole("heading", { level: 1, name: SESSIONS_USER.displayName }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { level: 2, name: "Sessions" }),
  ).toBeVisible();
  const table = page.getByRole("table", { name: "Active sessions" });
  await expect(table.getByRole("row")).toHaveCount(2); // header + the API sign-in
  await expect(table.getByText("This session")).toHaveCount(0);

  await page.getByRole("button", { name: "Sign out all sessions" }).click();
  const dialog = page.getByRole("alertdialog", {
    name: `Sign out all of ${SESSIONS_USER.username}'s sessions?`,
  });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Sign out all sessions" }).click();
  await expect(page.getByText("Signed out 1 session.").first()).toBeVisible();
  await expect(page.getByText("No active sessions")).toBeVisible();
  await expectCleanPage(page, issues, "user detail after signing out all", {
    h1: SESSIONS_USER.displayName,
  });
});

test("a non-admin sees no admin-only session control", async ({ page }) => {
  await signInAs(page, SESSIONS_USER);
  await openProfile(page);
  await expect(
    page.getByRole("button", { name: "Sign out all sessions" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Deactivate user" }),
  ).toHaveCount(0);

  // The admin's user page refuses the reader: no Sessions card, no sign-out control.
  await page.goto(`/admin/users/${adminUserId}`);
  await expect(
    page
      .getByRole("heading", { level: 1 })
      .or(page.getByRole("main").getByRole("alert"))
      .first(),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { level: 2, name: "Sessions" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Sign out all sessions" }),
  ).toHaveCount(0);
});
