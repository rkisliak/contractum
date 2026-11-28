// Contract authoring against the compose stack: create from the ODCS starter in the demo Ledger
// team (per-run name), the bundled Monaco editor renders under the CSP, validate, publish as the
// first (Latest) version, the versions tab and axe, then delete through the type-to-confirm dialog.
// Quick 261120-nqi: the draft and Document editors expand to a full-screen dialog (axe, Escape,
// focus back on "Expand editor"), and at 1440x1080 the Document editor fills the window without a
// page scrollbar.
import type { Page } from "@playwright/test";
import { exerciseDraftMessages } from "./draft-messages";
import {
  changeStatusToActiveAndSave,
  expect,
  expectNoSeriousA11yViolations,
  test,
} from "./fixtures";

function adminPassword(): string {
  const password = process.env.DC_UI_E2E_ADMIN_PASSWORD;
  if (!password)
    throw new Error("global-setup did not export DC_UI_E2E_ADMIN_PASSWORD");
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

test("create, validate, publish and delete a contract", async ({ page }) => {
  await signIn(page);
  await page.goto("/contracts/new");
  await expect(
    page.getByRole("heading", { level: 1, name: "New contract" }),
  ).toBeVisible();
  const folder = page.getByLabel("Folder", { exact: true });
  const options = await folder.locator("option").allTextContents();
  const ledger = options.find((option) => option.endsWith("/ledger"));
  test.skip(ledger === undefined, "the demo Ledger team is missing");
  await folder.selectOption({ label: ledger ?? "" });
  const name = `E2E orders ${Date.now().toString(36)}`;
  await page.getByLabel("Name").fill(name);
  const documentLoaded = page.waitForResponse(
    (response) =>
      /\/drafts\/[^/]+\/document$/.test(response.url()) &&
      response.request().method() === "GET",
  );
  await page.getByRole("button", { name: "Create contract" }).click();

  await expect(page).toHaveURL(/\/contracts\/[^/]+\/drafts\/[^/]+$/);
  await expect(page.locator(".monaco-editor").first()).toBeVisible();
  const original = await (await documentLoaded).text();

  // The draft editor opens full screen with its editing actions; Escape closes it.
  const expand = page.getByRole("button", { name: "Expand editor" });
  await expand.click();
  const draftDialog = page.getByRole("dialog", { name: / · Draft/ });
  await expect(draftDialog).toBeVisible();
  await expect(draftDialog.locator(".monaco-editor")).toBeVisible();
  for (const action of ["Save draft", "Validate draft", "Download buffer"])
    await expect(
      draftDialog.getByRole("button", { name: action }),
    ).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  await page.keyboard.press("Escape");
  await expect(draftDialog).toBeHidden();
  await expect(expand).toBeFocused();

  await page.getByRole("button", { name: "Validate draft" }).click();
  await expect(
    page.getByRole("heading", { name: "Validation findings" }),
  ).toBeVisible();

  // Quick 261119-wk8: the clearer refusal, finding and stale messages.
  await exerciseDraftMessages(page, original);

  await page.getByRole("button", { name: "Publish", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Publish 0.1.0?" });
  // The blank starter says `status: draft`: a released version needs `status: active`, so the
  // warning leads the dialog and Publish stays disabled.
  await expect(dialog).toContainText("A released version needs status: active");
  await expect(dialog).toContainText("This document says status: draft.");
  await expect(
    dialog.getByRole("button", { name: "Publish 0.1.0" }),
  ).toBeDisabled();
  await expectNoSeriousA11yViolations(page);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  // "Change status to active" rewrites the status line; the user saves, then publishes.
  await changeStatusToActiveAndSave(page);
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(dialog).not.toContainText("A released version");
  await dialog.getByRole("button", { name: "Publish 0.1.0" }).click();
  await expect(page.getByText("Published 0.1.0 as Latest")).toBeVisible();
  await expect(page.getByText(/Its document says status/)).toHaveCount(0);
  await expect(page.getByRole("cell", { name: "Latest" })).toBeVisible();
  await expectNoSeriousA11yViolations(page);

  // The stored publish report no longer carries the platform status warning.
  await page.getByRole("link", { name: "0.1.0", exact: true }).first().click();
  await page.getByRole("tab", { name: "Validation report" }).click();
  await expect(
    page.getByRole("region", { name: "Findings at publish" }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Findings at publish" }),
  ).not.toContainText("status is not 'active'");
  await expectNoSeriousA11yViolations(page);

  // The Document tab editor reaches the bottom of a 1440x1080 window with no page scrollbar,
  // and expands to a full-screen dialog with the format toggle and downloads.
  await page.setViewportSize({ width: 1440, height: 1080 });
  await page.getByRole("link", { name: "Document", exact: true }).click();
  const viewer = page.locator(".monaco-editor").first();
  await expect(viewer).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollHeight - window.innerHeight,
      ),
    )
    .toBeLessThanOrEqual(1);
  await expect
    .poll(async () => (await viewer.boundingBox())?.y ?? 0)
    .toBeGreaterThan(0);
  const box = await viewer.boundingBox();
  const innerHeight = await page.evaluate(() => window.innerHeight);
  expect((box?.y ?? 0) + (box?.height ?? 0)).toBeGreaterThan(innerHeight - 80);
  expect((box?.y ?? 0) + (box?.height ?? 0)).toBeLessThanOrEqual(innerHeight);
  await expand.click();
  const viewerDialog = page.getByRole("dialog", { name: / · Document$/ });
  await expect(viewerDialog).toBeVisible();
  await expect(
    viewerDialog.getByRole("group", { name: "Document format" }),
  ).toBeVisible();
  await expect(
    viewerDialog.getByRole("button", { name: "Download YAML" }),
  ).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  await page.keyboard.press("Escape");
  await expect(viewerDialog).toBeHidden();
  await expect(expand).toBeFocused();

  await page.getByRole("link", { name: "Overview" }).click();
  await page.getByRole("button", { name: "Delete contract" }).click();
  const confirm = page.getByRole("alertdialog");
  await confirm.getByRole("textbox").fill(name);
  await confirm.getByRole("button", { name: `Delete ${name}` }).click();
  await expect(page.getByText(`${name} was deleted`)).toBeVisible();
});
