// Admin > Settings against the real stack (quick 261120-0yc): each tab renders cleanly (axe, no
// console or API error), the default Entra tenant is saved, survives a reload and is reset, an
// announcement banner shows on Home and is cleared, Test connection answers, About lists the five
// services as OK, Export downloads a versioned file, importing it unchanged says "No changes" and
// importing a changed copy shows the diff and applies. A non-admin (the Ledger editor) has no
// Settings entry, sees NotAvailable and gets a 403 from the API. Every section is reset afterwards.
// Quick 261120-0ye adds the Governance and Sign-in & security tabs and the audit retention card: a
// lockout refusal (the signed-in local admin cannot turn local sign-in off), the password rules on
// /account/password, the lifecycle reason on the deprecate dialog and the retention setting.
import { readFileSync } from "node:fs";
import { request } from "@playwright/test";
import { expect, test } from "./fixtures";
import {
  adminApi,
  expectCleanPage,
  findDemo,
  IssueCollector,
  signIn,
} from "./journey-helpers";
import { ensurePersonas, LEDGER_EDITOR, signInAs } from "./personas";

const COMPOSE_TENANT = "11111111-2222-4333-8444-555555555555";
const GATEWAY_URL = process.env.DC_UI_E2E_SETUP_URL ?? "https://traefik";

test.use({ actionTimeout: 15_000, navigationTimeout: 30_000 });

type Section = { section: string; etag: string };

async function resetSettings(): Promise<void> {
  const api = await adminApi();
  try {
    const listed = await api.get<{ sections: Section[] }>("identity/settings");
    for (const section of listed.sections) {
      const res = await api.get<{ sections: Section[] }>("identity/settings");
      const current = res.sections.find((s) => s.section === section.section);
      if (current === undefined) continue;
      await api.putWithEtag(
        `identity/settings/${section.section}`,
        {},
        current.etag,
      );
    }
  } finally {
    await api.dispose();
  }
}

test.beforeAll(async () => {
  await resetSettings();
});
test.afterAll(async () => {
  await resetSettings();
});

test.describe.configure({ mode: "serial" });

test("an admin sees every tab render cleanly", async ({ page }) => {
  test.setTimeout(120_000);
  const issues = new IssueCollector(page);
  await signIn(page);
  for (const tab of [
    "entra",
    "notifications",
    "governance",
    "security",
    "platform",
  ]) {
    issues.reset();
    await page.goto(`/admin/settings?tab=${tab}`);
    await expectCleanPage(page, issues, `/admin/settings?tab=${tab}`, {
      h1: "Settings",
    });
  }
  await page.getByRole("tab", { name: "Platform" }).click();
  const table = page.getByRole("table", { name: "Services" });
  await expect(table.getByRole("rowheader")).toHaveCount(6); // ui plus five services
  await expect(table.getByText("OK", { exact: true })).toHaveCount(6);
});

test("the default tenant is saved, persists a reload and is reset", async ({
  page,
}) => {
  await signIn(page);
  await page.goto("/admin/settings?tab=entra");
  const tenant = page.getByLabel("Default tenant", { exact: true });
  await tenant.selectOption(COMPOSE_TENANT);
  await page.getByRole("button", { name: "Save Entra defaults" }).click();
  await expect(page.getByText("Entra defaults saved.")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Default tenant", { exact: true })).toHaveValue(
    COMPOSE_TENANT,
  );
  await page
    .getByRole("button", { name: "Reset to default for default tenant" })
    .click();
  await expect(page.getByLabel("Default tenant", { exact: true })).toHaveValue(
    "",
  );
});

test("an info banner shows on Home and is cleared", async ({ page }) => {
  await signIn(page);
  await page.goto("/admin/settings?tab=platform");
  await page.getByLabel("Banner text").fill("Planned maintenance tonight");
  await page.getByRole("button", { name: "Save banner" }).click();
  await expect(page.getByText("Banner saved.")).toBeVisible();
  await page.goto("/");
  await expect(
    page.getByText("Planned maintenance tonight").first(),
  ).toBeVisible();
  await page.goto("/admin/settings?tab=platform");
  await page
    .getByRole("button", { name: "Reset to default", exact: true })
    .click();
  await expect(page.getByText("Banner cleared.")).toBeVisible();
  await page.goto("/");
  await expect(page.getByText("Planned maintenance tonight")).toHaveCount(0);
});

test("Test connection shows a result", async ({ page }) => {
  await signIn(page);
  await page.goto("/admin/settings?tab=entra");
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByText("Connection test")).toBeVisible();
});

test("export downloads a versioned file and an import round-trips", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  await page.goto("/admin/settings?tab=platform");
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export" }).click(),
  ]);
  const path = await download.path();
  const exported = JSON.parse(readFileSync(path, "utf8")) as {
    format: string;
    format_version: number;
    sections: Record<string, Record<string, unknown>>;
  };
  expect(exported.format).toBe("contractum-settings");
  expect(exported.format_version).toBe(1);
  expect(download.suggestedFilename()).toMatch(
    /^contractum-settings-.*\.json$/,
  );

  // The unchanged file says "No changes".
  await page.getByRole("button", { name: "Import" }).click();
  const dialog = page.getByRole("dialog", { name: "Import settings" });
  await dialog.getByLabel("Settings file (.json)").setInputFiles(path);
  await expect(dialog.getByText("No changes")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Apply" })).toBeDisabled();
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // A copy with a changed banner shows the diff, and Apply works.
  const changed = {
    ...exported,
    sections: {
      ...exported.sections,
      platform: { banner: { text: "Imported banner", severity: "info" } },
    },
  };
  await page.getByRole("button", { name: "Import" }).click();
  const second = page.getByRole("dialog", { name: "Import settings" });
  await second.getByLabel("Settings file (.json)").setInputFiles({
    name: "settings.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(changed)),
  });
  await expect(second.getByRole("table")).toBeVisible();
  await expect(
    second.getByRole("cell", { name: "banner", exact: true }),
  ).toBeVisible();
  await second.getByRole("button", { name: "Apply" }).click();
  await expect(second).toBeHidden();
  await page.goto("/");
  await expect(page.getByText("Imported banner").first()).toBeVisible();
});

// Notifications (quick 261120-0yd). The mail catcher (mailpit) is on the backend network only and
// the UI e2e runner is on the edge network, so the message itself is not read here: the UI outcome
// "Test email sent" is asserted (the mail service answered 200 with an accepted recipient).
test("the notification templates preview, reject an unknown placeholder and reset", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  await page.goto("/admin/settings?tab=notifications");
  await page.getByLabel("Notice").selectOption("contract.version.published");
  const subject = page.getByLabel("Subject", { exact: true });
  const defaultSubject = await subject.inputValue();
  expect(defaultSubject).toContain("{odcs}");

  await subject.fill("{card}: {odcs} {semver} published");
  await page.getByRole("button", { name: "Preview" }).click();
  await expect(page.getByTestId("template-preview")).toContainText(
    "Analytics team: orders 1.2.0 published",
  );

  await subject.fill("{bogus} published");
  await page.getByRole("button", { name: "Preview" }).click();
  await expect(page.getByText("Unknown placeholder 'bogus'.")).toBeVisible();

  await subject.fill("{card}: {odcs} {semver} published");
  await page
    .getByRole("button", { name: "Save notification settings" })
    .click();
  await expect(page.getByText("Notification settings saved.")).toBeVisible();
  await page.reload();
  await page.getByLabel("Notice").selectOption("contract.version.published");
  await expect(page.getByLabel("Subject", { exact: true })).toHaveValue(
    "{card}: {odcs} {semver} published",
  );

  await page
    .getByRole("button", { name: "Reset to default for this template" })
    .click();
  await expect(page.getByText("Template reset to default.")).toBeVisible();
  await expect(page.getByLabel("Subject", { exact: true })).toHaveValue(
    defaultSubject,
  );
});

test("the sender is saved and a test email is sent", async ({ page }) => {
  test.setTimeout(120_000);
  await signIn(page);
  await page.goto("/admin/settings?tab=notifications");
  await page.getByLabel(/Sender name/).fill("Contractum E2E");
  await page
    .getByRole("button", { name: "Save notification settings" })
    .click();
  await expect(page.getByText("Notification settings saved.")).toBeVisible();

  await page.getByRole("button", { name: "Send test email" }).click();
  const dialog = page.getByRole("dialog", { name: "Send a test email" });
  await dialog.getByLabel("Send to").fill("settings-test@example.com");
  await dialog.getByRole("button", { name: "Send test email" }).click();
  await expect(dialog.getByText("Test email sent")).toBeVisible();
});

test("an empty allowed-hosts list says that no webhook host is allowed", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  await page.goto("/admin/settings?tab=notifications");
  const hosts = page.getByLabel("Allowed webhook hosts");
  await hosts.fill("");
  await expect(
    page.getByText(
      "No webhook host is allowed, so card owners cannot add webhooks and none are sent.",
    ),
  ).toBeVisible();
  await hosts.fill("hooks.example.org");
  await expect(page.getByText(/card owners cannot add webhooks/)).toBeHidden();
});

test("the allowed webhook hosts narrow a card webhook", async ({ page }) => {
  test.setTimeout(120_000);
  await signIn(page);
  await page.goto("/admin/settings?tab=notifications");
  await expect(page.getByText("Enforced")).toBeVisible();
  await page.getByLabel("Allowed webhook hosts").fill("*.example.org");
  await page
    .getByRole("button", { name: "Save notification settings" })
    .click();
  await expect(page.getByText("Notification settings saved.")).toBeVisible();

  const api = await adminApi();
  try {
    const demo = await findDemo(api);
    expect(demo.card).not.toBeNull();
    // The settings are read with a 30 s cache and identity's own 5 s cache is cleared on a write.
    await expect
      .poll(
        async () =>
          (
            await api.patchProblem(`contracts/cards/${demo.card?.id ?? ""}`, {
              webhook_url: "https://hooks.example.net/x",
            })
          ).type,
        { timeout: 45_000 },
      )
      .toBe("urn:dc:problem:webhook-host-not-allowed");
    const refused = await api.patchProblem(
      `contracts/cards/${demo.card?.id ?? ""}`,
      { webhook_url: "https://hooks.example.net/x" },
    );
    expect(refused.status).toBe(422);

    // The card dialog says why, and a Global admin gets the way to fix it.
    await page.goto(`/cards/${demo.card?.id ?? ""}`);
    await page.getByRole("button", { name: "Edit card" }).click();
    const dialog = page.getByRole("dialog", { name: "Edit card" });
    await dialog.getByLabel("Deliver notices to a webhook").check();
    await dialog.getByLabel(/Webhook URL/).fill("https://hooks.example.net/x");
    await dialog.getByRole("button", { name: "Save card" }).click();
    await expect(
      dialog.getByText(
        "This host is not on the list of allowed webhook hosts.",
      ),
    ).toBeVisible();
    await expect(
      dialog.getByRole("link", { name: "Add it in Settings" }),
    ).toHaveAttribute("href", "/admin/settings?tab=notifications");
  } finally {
    await api.dispose();
  }
});

test("the signed-in local admin cannot turn local sign-in off", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  await page.goto("/admin/settings?tab=security");
  const local = page.getByLabel("Local sign-in", { exact: true });
  await expect(local).toBeChecked();
  await local.uncheck();
  await page
    .getByRole("button", { name: "Save sign-in and security settings" })
    .click();
  await expect(page.getByText("This change could lock you out")).toBeVisible();
  await expect(page.getByText(/Nothing was changed\./)).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Local sign-in", { exact: true })).toBeChecked();
});

test("the password minimum is listed on the change-password screen, then reset", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  await page.goto("/admin/settings?tab=security");
  await page.getByLabel("Minimum length").fill("14");
  await page
    .getByRole("button", { name: "Save sign-in and security settings" })
    .click();
  await expect(
    page.getByText("Sign-in and security settings saved."),
  ).toBeVisible();

  await page.goto("/account/password");
  await expect(
    page
      .getByRole("list", { name: "Password rules" })
      .getByText("At least 14 characters"),
  ).toBeVisible();

  await page.goto("/admin/settings?tab=security");
  await page
    .getByRole("button", { name: "Reset to default for password rules" })
    .click();
  await expect(page.getByText("Password rules reset.")).toBeVisible();
  await page.goto("/account/password");
  await expect(
    page
      .getByRole("list", { name: "Password rules" })
      .getByText("At least 12 characters"),
  ).toBeVisible();
});

test("requiring a reason makes the deprecate dialog ask for one", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const api = await adminApi();
  let orders: string;
  try {
    orders = (await findDemo(api)).orders.id;
  } finally {
    await api.dispose();
  }
  await signIn(page);
  await page.goto("/admin/settings?tab=governance");
  await page.getByLabel("Require a reason to unpublish or deprecate").check();
  await page.getByRole("button", { name: "Save governance settings" }).click();
  await expect(page.getByText("Governance settings saved.")).toBeVisible();

  await page.goto(`/contracts/${orders}/versions`);
  await page.getByRole("button", { name: "Actions for 1.1.0" }).click();
  await page.getByRole("menuitem", { name: "Deprecate version" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Deprecate 1.1.0?" });
  await expect(dialog.getByLabel("Reason", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Optional")).toHaveCount(0);
  // Never deprecate demo data: leave without confirming.
  await dialog.getByRole("button", { name: "Keep 1.1.0" }).click();
  await expect(dialog).toHaveCount(0);

  await page.goto("/admin/settings?tab=governance");
  await page
    .getByRole("button", {
      name: "Reset to default for the reason requirement",
    })
    .click();
  await expect(page.getByText("Reason requirement reset.")).toBeVisible();
});

test("the audit retention is set to 400 days and reset", async ({ page }) => {
  test.setTimeout(120_000);
  await signIn(page);
  await page.goto("/admin/settings?tab=platform");
  await page.getByRole("button", { name: "Delete after N days" }).click();
  await page.getByLabel("Delete events older than (days)").fill("400");
  await page.getByRole("button", { name: "Save retention" }).click();
  await expect(page.getByText("Audit retention saved.")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Delete events older than (days)")).toHaveValue(
    "400",
  );
  await page
    .getByRole("button", { name: "Reset to default for audit retention" })
    .click();
  await expect(page.getByText("Audit retention reset.")).toBeVisible();
});

test("a non-admin has no Settings entry and the API refuses them", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const api = await adminApi();
  try {
    await ensurePersonas(api);
  } finally {
    await api.dispose();
  }
  await signInAs(page, LEDGER_EDITOR);
  const nav = page.getByRole("navigation", { name: "Primary" });
  await expect(
    nav.getByRole("link", { name: "Settings", exact: true }),
  ).toHaveCount(0);
  await page.goto("/admin/settings");
  await expect(
    page.getByText("Not available at your access level"),
  ).toBeVisible();

  // The recorded refusal for every non-admin caller of a ROOT permission is 403.
  const ctx = await request.newContext({
    baseURL: GATEWAY_URL,
    ignoreHTTPSErrors: true,
  });
  try {
    const login = await ctx.post("/api/v1/identity/auth/login", {
      data: {
        username: LEDGER_EDITOR.username,
        password: LEDGER_EDITOR.password,
      },
    });
    const { access_token } = (await login.json()) as { access_token: string };
    const refused = await ctx.get("/api/v1/identity/settings", {
      headers: { Authorization: `Bearer ${access_token}` },
    });
    expect(refused.status()).toBe(403);
  } finally {
    await ctx.dispose();
  }
});
