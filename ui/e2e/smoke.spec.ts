import {
  expect,
  expectNoHorizontalOverflow,
  expectNoSeriousA11yViolations,
  test,
} from "./fixtures";

const ADMIN_USERNAME = "admin";

function adminPassword(): string {
  const password = process.env.DC_UI_E2E_ADMIN_PASSWORD;
  if (!password) {
    throw new Error("global-setup did not export DC_UI_E2E_ADMIN_PASSWORD");
  }
  return password;
}

test("the app is served at / with strict headers", async ({ page }) => {
  const response = await page.goto("/");
  if (response === null)
    throw new Error("navigation to / returned no response");
  const headers = response.headers();
  const csp = headers["content-security-policy"] ?? "";
  expect(csp).toContain("frame-ancestors 'none'");
  expect(csp).toContain("script-src 'self'");
  expect(headers["strict-transport-security"]).toBeTruthy();

  await expect(
    page.getByText("Contractum", { exact: true }).first(),
  ).toBeVisible();

  // Fetched inside the page: Chromium honours the host mapping, Playwright's Node-side request
  // client does not (it would try localhost:8443 in the runner itself).
  const body = await page.evaluate(async () => {
    const res = await fetch("/config.json");
    if (!res.ok) throw new Error(`config.json answered ${String(res.status)}`);
    return (await res.json()) as Record<string, unknown>;
  });
  for (const key of Object.keys(body)) {
    expect(key).not.toMatch(/secret/i);
  }
});

test("/ has no serious accessibility violation", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByText("Contractum", { exact: true }).first(),
  ).toBeVisible();
  await expectNoSeriousA11yViolations(page);
});

test("/ reflows at 320 px without horizontal scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/");
  await expect(
    page.getByText("Contractum", { exact: true }).first(),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("a Global admin signs in locally and sees their name", async ({
  page,
  context,
}) => {
  // Without a session "/" goes to the sign-in page and remembers where the user was heading.
  await page.goto("/");
  await expect(page).toHaveURL(/\/signin\?redirect=%2F$/);

  await page.getByLabel("Username").fill(ADMIN_USERNAME);
  await page.getByLabel("Password", { exact: true }).fill(adminPassword());
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  await expect(page).toHaveURL(/\/$/);
  await expect(
    page.getByRole("heading", { level: 1, name: `Welcome, ${ADMIN_USERNAME}` }),
  ).toBeVisible();

  // The access token is only in memory: nothing in Web Storage, and the refresh token is an
  // HttpOnly cookie that page script cannot read (T-8-24).
  const exposed = await page.evaluate(() => ({
    local: Object.keys(localStorage).length,
    // MSAL (initialized at boot when Entra is enabled) keeps its own bookkeeping under `msal.`
    // keys in sessionStorage (D-UI-08-16); nothing else may be there, and no value holds a token.
    session: Object.keys(sessionStorage).filter(
      (key) => !key.startsWith("msal."),
    ).length,
    sessionValues: Object.values(sessionStorage).join(" "),
    cookie: document.cookie,
    url: location.href,
  }));
  expect(exposed.local).toBe(0);
  expect(exposed.session).toBe(0);
  expect(exposed.sessionValues).not.toMatch(/eyJ[\w-]+\.eyJ/);
  expect(exposed.cookie).not.toContain("dc_refresh");
  expect(exposed.url).not.toMatch(/token|eyJ/i);
  const refresh = (await context.cookies()).find(
    (cookie) => cookie.name === "__Secure-dc_refresh",
  );
  expect(refresh?.httpOnly).toBe(true);
  expect(refresh?.secure).toBe(true);

  // A reload restores the session from the cookie without flashing the sign-in page.
  await page.reload();
  await expect(
    page.getByRole("heading", { level: 1, name: `Welcome, ${ADMIN_USERNAME}` }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
});

test("a wrong password shows the generic message and keeps the username", async ({
  page,
}) => {
  await page.goto("/signin");
  await page.getByLabel("Username").fill(ADMIN_USERNAME);
  await page.getByLabel("Password", { exact: true }).fill("not-the-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  const alert = page.getByRole("alert").filter({
    hasText: "Username or password is incorrect.",
  });
  await expect(alert).toBeVisible();
  await expect(page.getByLabel("Username")).toHaveValue(ADMIN_USERNAME);
  const password = page.getByLabel("Password", { exact: true });
  await expect(password).toHaveValue("");
  await expect(password).toBeFocused();
  // One generic failure: never the wording of an unknown user or a wrong password.
  await expect(
    page.getByText(/not found|unknown user|no such user/i),
  ).toHaveCount(0);
  await expect(page).toHaveURL(/\/signin/);
});

test("an unknown username gets the same message as a wrong password", async ({
  page,
}) => {
  await page.goto("/signin");
  await page.getByLabel("Username").fill("nobody-by-this-name");
  await page.getByLabel("Password", { exact: true }).fill("not-the-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({
      hasText: "Username or password is incorrect.",
    }),
  ).toBeVisible();
});

test("an empty submit asks for the username first", async ({ page }) => {
  await page.goto("/signin");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Enter your username.")).toBeVisible();
  await expect(page.getByText("Enter your password.")).toBeVisible();
  await expect(page.getByLabel("Username")).toBeFocused();
});

test("/signin has the brand panel, one h1 and no serious accessibility violation", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/signin");
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Sign in");
  const aside = page.getByRole("complementary", {
    name: "About Contractum",
  });
  await expect(aside).toBeVisible();
  await expect(aside.getByRole("heading")).toHaveCount(0);
  await expect(page).toHaveTitle("Sign in · Contractum");
  await expectNoSeriousA11yViolations(page);

  // The failure state is part of the page's contract too.
  await page.getByLabel("Username").fill(ADMIN_USERNAME);
  await page.getByLabel("Password", { exact: true }).fill("not-the-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expectNoSeriousA11yViolations(page);
});

test("the bootstrap rotation landed the admin signed in", async ({ page }) => {
  test.skip(
    process.env.DC_UI_E2E_ROTATED !== "1",
    "bootstrap already consumed on this warm stack; runs in the cold gate",
  );
  // global-setup drove /signin/rotate in a browser; the stored password now signs in normally.
  await page.goto("/signin");
  await page.getByLabel("Username").fill(ADMIN_USERNAME);
  await page.getByLabel("Password", { exact: true }).fill(adminPassword());
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: `Welcome, ${ADMIN_USERNAME}` }),
  ).toBeVisible();
});

test("/signin/rotate without a restricted session restarts at sign-in", async ({
  page,
}) => {
  await page.goto("/signin/rotate");
  await expect(page).toHaveURL(/\/signin\?notice=rotation-restart$/);
  await expect(
    page.getByText(
      "Sign in with your one-time password again to set a new password.",
    ),
  ).toBeVisible();
});

test("/signin/rotate renders accessibly for a restricted sign-in", async ({
  page,
}) => {
  // The login answer is stubbed with a restricted scope, so nothing real is rotated: the form is
  // only rendered and checked, never submitted.
  await page.route("**/api/v1/identity/auth/login", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        access_token: "restricted-stub",
        expires_in: 300,
        scope: "password:rotate",
        token_type: "Bearer",
      }),
    }),
  );
  await page.goto("/signin");
  await page.getByLabel("Username").fill(ADMIN_USERNAME);
  await page.getByLabel("Password", { exact: true }).fill("one-time-stub");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Set a new password" }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/signin\/rotate$/);
  await expect(page).toHaveTitle("Set a new password · Contractum");
  await page.getByRole("button", { name: "Set new password" }).click();
  await expect(
    page.getByText("Use at least 12 characters.", { exact: true }),
  ).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  await page.getByRole("button", { name: "Back to sign in" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Sign in" }),
  ).toBeVisible();
});

test("/signin reflows at 320 px without horizontal scrolling", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/signin");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("a redirect to another origin is never followed after sign-in", async ({
  page,
}) => {
  await page.goto("/signin?redirect=//evil.example/x");
  await page.getByLabel("Username").fill(ADMIN_USERNAME);
  await page.getByLabel("Password", { exact: true }).fill(adminPassword());
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/localhost:8443\/$/);
  await expect(
    page.getByRole("heading", { level: 1, name: /^Welcome, / }),
  ).toBeVisible();
});

test("two tabs reloading together stay signed in", async ({
  page,
  context,
}) => {
  await page.goto("/signin");
  await page.getByLabel("Username").fill(ADMIN_USERNAME);
  await page.getByLabel("Password", { exact: true }).fill(adminPassword());
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const welcome = (target: typeof page) =>
    target.getByRole("heading", {
      level: 1,
      name: `Welcome, ${ADMIN_USERNAME}`,
    });
  await expect(welcome(page)).toBeVisible();

  // Both tabs restore from the same one-time cookie at the same moment: the Web Lock makes them
  // take turns, so neither presents a rotated token (which would revoke the whole family).
  const second = await context.newPage();
  await second.goto("/");
  await expect(welcome(second)).toBeVisible();
  await Promise.all([page.reload(), second.reload()]);
  await expect(welcome(page)).toBeVisible();
  await expect(welcome(second)).toBeVisible();

  // The family is still alive: a further reload restores again.
  await page.reload();
  await expect(welcome(page)).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
});

test("a reload keeps the session and the refresh cookie is not readable", async ({
  page,
  context,
}) => {
  await page.goto("/signin");
  await page.getByLabel("Username").fill(ADMIN_USERNAME);
  await page.getByLabel("Password", { exact: true }).fill(adminPassword());
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const welcome = page.getByRole("heading", {
    level: 1,
    name: `Welcome, ${ADMIN_USERNAME}`,
  });
  await expect(welcome).toBeVisible();

  // From here on the sign-in page must never appear: record every navigation.
  const visited: string[] = [];
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) visited.push(frame.url());
  });
  await page.reload();
  await expect(welcome).toBeVisible();
  expect(visited.filter((url) => url.includes("/signin"))).toEqual([]);

  expect(await page.evaluate(() => document.cookie)).not.toContain(
    "dc_refresh",
  );
  const refresh = (await context.cookies()).find(
    (cookie) => cookie.name === "__Secure-dc_refresh",
  );
  expect(refresh?.httpOnly).toBe(true);
  expect(refresh?.sameSite).toBe("Strict");
});

test("a deep link survives sign-in", async ({ page }) => {
  await page.goto("/?x=1");
  await expect(page).toHaveURL(/\/signin\?redirect=/);
  await page.getByLabel("Username").fill(ADMIN_USERNAME);
  await page.getByLabel("Password", { exact: true }).fill(adminPassword());
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/\?x=1$/);
  await expect(
    page.getByRole("heading", { level: 1, name: /^Welcome, / }),
  ).toBeVisible();
});

test("a signed-in user visiting /signin goes to the redirect target or /", async ({
  page,
}) => {
  await page.goto("/signin");
  await page.getByLabel("Username").fill(ADMIN_USERNAME);
  await page.getByLabel("Password", { exact: true }).fill(adminPassword());
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    /^Welcome, /,
  );
  await page.goto("/signin?redirect=%2F%3Fx%3D2");
  await expect(page).toHaveURL(/\/\?x=2$/);
  await page.goto("/signin?redirect=//evil.example");
  await expect(page).toHaveURL(/localhost:8443\/$/);
});

test("an unknown notice renders nothing and a known one its fixed copy", async ({
  page,
}) => {
  await page.goto("/signin?notice=%3Cb%3Ex%3C%2Fb%3E");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Sign in");
  await expect(page.getByText("Information", { exact: true })).toHaveCount(0);
  await page.goto("/signin?notice=session-expired");
  await expect(
    page.getByText("Your session expired. Sign in again."),
  ).toBeVisible();
});

// Self-check: the automatic collector must fail a test that provokes a violation. test.fail
// inverts the result, so this passes only when the fixture really fails the test. The assertion in
// the body is deliberately independent of that collector: under test.fail a failing in-body
// assertion would be inverted to a pass and hide a broken collector, so the body only proves, with
// its own listener, that the provocation is a real violation event reaching document listeners.
test.describe("the CSP collector is wired", () => {
  test.fail();
  test("a dispatched violation event fails the test", async ({ page }) => {
    await page.goto("/");
    const seen = await page.evaluate(() => {
      let directive: string | null = null;
      document.addEventListener(
        "securitypolicyviolation",
        (event) => {
          directive = event.violatedDirective;
        },
        { once: true },
      );
      document.dispatchEvent(
        new SecurityPolicyViolationEvent("securitypolicyviolation", {
          violatedDirective: "script-src",
          blockedURI: "https://evil.example/x.js",
        }),
      );
      return directive;
    });
    expect(seen).toBe("script-src");
  });
});
