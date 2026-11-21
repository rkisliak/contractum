// Entra sign-in through the compose mock (D-UI-08-16, A-3, A-4): MSAL v5 redirect with PKCE, the
// /redirect.html bridge back to /auth/callback, the token in the shared store, and the shell as
// the Entra persona Alice (client_id mapping `ui-alice`). Alice is admitted through the Data Eng
// group registration made in global-setup. Also: an unmapped client id ends on the failure card,
// and the bridge page carries its own header set (frame-ancestors 'self', no COOP).
import { expect, expectNoSeriousA11yViolations, test } from "./fixtures";

test.use({ actionTimeout: 15_000 });

test("an Entra persona signs in through the mock", async ({ page }) => {
  await page.goto("/signin");
  const microsoft = page.getByRole("button", {
    name: "Sign in with Microsoft",
  });
  await expect(microsoft).toBeVisible();
  await expect(page.getByText("or", { exact: true })).toBeVisible();
  await expectNoSeriousA11yViolations(page);

  await microsoft.click();
  await expect(page).toHaveURL(/\/$/, { timeout: 30_000 });
  await expect(
    page.getByRole("heading", { level: 1, name: "Welcome, Alice" }),
  ).toBeVisible();
  await expect(page.getByText("Microsoft Entra").first()).toBeVisible();

  // The API access token is never put in the URL; MSAL's own cache is per tab (D-UI-08-16).
  expect(page.url()).not.toMatch(/code=|token|eyJ/i);

  // A reload restores the Entra session silently from MSAL's cache.
  await page.reload();
  await expect(
    page.getByRole("heading", { level: 1, name: "Welcome, Alice" }),
  ).toBeVisible({ timeout: 30_000 });
});

test("a Microsoft sign-in the platform refuses ends on the failure card", async ({
  page,
}) => {
  // No mock mapping for this client id: the token carries no tenant or audience, so the API
  // refuses it and the callback shows why.
  // The compose values with the persona swapped (A-3). Fulfilled here: Playwright's Node side
  // cannot reach the browser-mapped origin to fetch the real file.
  await page.route("**/config.json", (route) =>
    route.fulfill({
      json: {
        entraEnabled: true,
        entraClientId: "ui-nobody",
        entraAuthority: "https://localhost:8443/entra",
        entraScopes: "openid profile offline_access",
        entraProtocol: "oidc",
        localLoginEnabled: true,
        apiBase: "/api/v1",
      },
      headers: { "Cache-Control": "no-store" },
    }),
  );
  await page.goto("/signin");
  await page.getByRole("button", { name: "Sign in with Microsoft" }).click();
  await expect(page).toHaveURL(/\/auth\/callback/, { timeout: 30_000 });
  await expect(
    page.getByRole("button", { name: "Try loading again" }),
  ).toBeVisible({ timeout: 30_000 });
  await expect(
    page.getByRole("button", { name: "Back to sign in" }),
  ).toBeVisible();
  await expect(page.getByRole("alert")).toBeVisible();
  await expectNoSeriousA11yViolations(page);
});

test("the redirect bridge has its own frame and opener headers", async ({
  page,
}) => {
  const bridge = await page.goto("/redirect.html");
  if (bridge === null) throw new Error("no response for /redirect.html");
  const headers = bridge.headers();
  expect(headers["content-security-policy"]).toContain(
    "frame-ancestors 'self'",
  );
  expect(headers["cross-origin-opener-policy"]).toBeUndefined();
  const app = await page.goto("/signin");
  if (app === null) throw new Error("no response for /signin");
  expect(app.headers()["content-security-policy"]).toContain(
    "frame-ancestors 'none'",
  );
  expect(app.headers()["cross-origin-opener-policy"]).toBe("same-origin");
});
