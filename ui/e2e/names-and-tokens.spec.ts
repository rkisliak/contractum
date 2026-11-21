// The name rule and the active-token limit against the real stack (quick 261120-lbz, D-01 and D-03).
// (a) the Create folder dialog and (b) the starter name on New contract show the rule's message for
// "Ops 😀" before submit and send nothing, as the Global admin and as a non-admin persona (the
// Ledger editor); (c) as the Global admin the token limit is set (Admin > Settings > Sign-in &
// security) to the admin's current active count, "Create token" is disabled on /account/tokens with
// the explanation in text, then the limit is reset and what the spec created is revoked; (d) the
// non-admin persona's tokens page shows the counter. Axe runs on every state.
import { expect, test } from "./fixtures";
import {
  adminApi,
  findDemo,
  IssueCollector,
  signIn,
  softAxe,
} from "./journey-helpers";
import {
  ensurePersonas,
  LEDGER_EDITOR,
  openAccountMenu,
  signInAs,
  signOut,
} from "./personas";

const MESSAGE =
  "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.";

test.use({ actionTimeout: 15_000, navigationTimeout: 30_000 });
test.describe.configure({ mode: "serial" });

type PatPage = {
  items: { id: string; name: string; status: string }[];
  active_count: number;
  max_active: number;
};
type Section = { section: string; etag: string };

const CREATED_PREFIX = "names-tokens-spec";

async function revokeSpecTokens(): Promise<void> {
  const api = await adminApi();
  try {
    const page = await api.get<PatPage>("identity/pats?limit=100");
    for (const pat of page.items) {
      if (pat.name.startsWith(CREATED_PREFIX) && pat.status === "active")
        await api.delete(`identity/pats/${pat.id}`);
    }
  } finally {
    await api.dispose();
  }
}

async function resetSecurity(): Promise<void> {
  const api = await adminApi();
  try {
    const listed = await api.get<{ sections: Section[] }>("identity/settings");
    const current = listed.sections.find((s) => s.section === "security");
    if (current !== undefined)
      await api.putWithEtag("identity/settings/security", {}, current.etag);
  } finally {
    await api.dispose();
  }
}

test.beforeAll(async () => {
  await ensurePersonas(await adminApi());
});
test.afterAll(async () => {
  await resetSecurity();
  await revokeSpecTokens();
});

for (const who of ["admin", "editor"] as const) {
  test(`the name rule shows its message before submit (${who})`, async ({
    page,
  }) => {
    test.setTimeout(120_000);
    const api = await adminApi();
    const demo = await findDemo(api);
    await api.dispose();
    const issues = new IssueCollector(page);
    if (who === "admin") await signIn(page);
    else await signInAs(page, LEDGER_EDITOR);
    const writes: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().includes("/api/v1/"))
        writes.push(request.url());
    });

    if (who === "admin") {
      // (a) Create project in Acme: an emoji in the name.
      await page.goto(`/folders/${demo.acme.id}`);
      await page
        .getByRole("button", { name: "Create project" })
        .first()
        .click();
      const dialog = page.getByRole("dialog", { name: "Create project" });
      await dialog.getByLabel("Name").fill("Ops \u{1F600}");
      await expect(dialog.getByText(MESSAGE)).toBeVisible();
      await softAxe(page, "create folder with a refused name");
      await dialog.getByRole("button", { name: "Create project" }).click();
      await expect(dialog.getByText(MESSAGE)).toBeVisible();
      expect(
        writes.filter((url) => url.endsWith("/identity/folders")),
        "no folder was created",
      ).toEqual([]);
      await page.keyboard.press("Escape");
    }

    // (b) The starter name on New contract.
    await page.goto(`/contracts/new?folder=${demo.ledger.id}`);
    await page.getByLabel("ODCS starter").check();
    await page.getByLabel("Name", { exact: true }).fill("Orders \u{1F600}");
    await expect(page.getByText(MESSAGE)).toBeVisible();
    await softAxe(page, "new contract with a refused name");
    await page.getByRole("button", { name: "Create contract" }).click();
    await expect(page.getByText(MESSAGE)).toBeVisible();
    expect(
      writes.filter((url) => url.includes("/contracts/starter")),
      "no contract was created",
    ).toEqual([]);
    expect(issues.take(), "console, page or API errors").toEqual([]);
    if (who === "editor") await signOut(page);
  });
}

test("the token limit disables Create token with the explanation, then resets", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const api = await adminApi();
  try {
    let usage = await api.get<PatPage>("identity/pats?limit=1");
    if (usage.active_count === 0) {
      await api.post("identity/pats", {
        name: `${CREATED_PREFIX}-${Date.now().toString(36)}`,
        expires_at: new Date(Date.now() + 7 * 86_400_000).toISOString(),
      });
      usage = await api.get<PatPage>("identity/pats?limit=1");
    }
    const active = usage.active_count;
    await signIn(page);
    await page.goto("/admin/settings?tab=security");
    const limit = page.getByLabel("Maximum active access tokens per owner");
    await expect(limit).toBeVisible();
    await softAxe(page, "security settings with the token limit");
    await limit.fill(String(active));
    await page
      .getByRole("button", { name: "Save sign-in and security settings" })
      .click();
    await expect(
      page.getByText("Sign-in and security settings saved."),
    ).toBeVisible();

    await page.goto("/account/tokens");
    await expect(
      page.getByText(
        `${String(active)} of ${String(active)} active. You have reached the limit of ${String(active)} active access tokens. Revoke one to create another.`,
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Create token" }),
    ).toBeDisabled();
    await softAxe(page, "tokens page at the limit");

    await page.goto("/admin/settings?tab=security");
    await page
      .getByRole("button", { name: "Reset to default for access tokens" })
      .click();
    await expect(page.getByText("Access token settings reset.")).toBeVisible();
    await page.goto("/account/tokens");
    await expect(
      page.getByText(new RegExp(`^${String(active)} of 10 active`)),
    ).toBeVisible();
  } finally {
    await api.dispose();
  }
});

test("a non-admin sees the counter on the tokens page", async ({ page }) => {
  test.setTimeout(120_000);
  const issues = new IssueCollector(page);
  await signInAs(page, LEDGER_EDITOR);
  await page.goto("/account/tokens");
  await expect(
    page.getByRole("heading", { level: 1, name: "Access tokens" }),
  ).toBeVisible();
  await expect(page.getByText(/^\d+ of \d+ active/)).toBeVisible();
  await softAxe(page, "non-admin tokens page");
  await expect(
    page.getByRole("button", { name: "Create token" }),
  ).toBeEnabled();
  expect(issues.take(), "console, page or API errors").toEqual([]);
  await openAccountMenu(page);
  await page.keyboard.press("Escape");
});
