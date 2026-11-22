// The clearer draft messages (quick 261119-wk8), driven in a real browser from an open draft
// editor: a pasted document with another id names the expected id and "Put the contract's id back"
// fixes it, an extra top-level key is named in the finding, and findings of an older revision are
// shown as stale. Ends with the original document saved again so the caller can go on to publish.
import type { Page } from "@playwright/test";
import { expect, pasteIntoEditor } from "./fixtures";

export type Shot = (name: string) => Promise<void>;

// Monaco takes pasted text as is (typing would auto-indent), as design-screens.spec.ts does.
async function replaceBuffer(page: Page, text: string) {
  await pasteIntoEditor(page, text);
}

export async function exerciseDraftMessages(
  page: Page,
  original: string,
  shot?: Shot,
): Promise<void> {
  expect(original).toMatch(/^id:/m);

  // 1. A document with another id: both ids are named and one click puts the contract's back.
  await replaceBuffer(
    page,
    original.replace(/^id:.*$/m, "id: urn:pasted:sample"),
  );
  await page.getByRole("button", { name: "Save draft" }).click();
  const alert = page.getByRole("alert").filter({
    hasText: "the document id does not match this contract",
  });
  await expect(alert).toContainText("This contract's id is ");
  await expect(alert).toContainText("The document says urn:pasted:sample.");
  await shot?.("id-mismatch-alert");
  await alert
    .getByRole("button", { name: "Put the contract's id back" })
    .click();
  await expect(alert).toHaveCount(0);
  // Nothing was saved by the button; the buffer is the stored document again.
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();

  // 2. An extra top-level key: the structural finding names it (the key, never a value).
  await replaceBuffer(page, `${original.trimEnd()}\nowner-team: payments\n`);
  await page.getByRole("button", { name: "Save draft" }).click();
  const panel = page.getByRole("region", { name: "Validation findings" });
  await expect(panel).toContainText(
    "The object has properties the schema does not allow: `owner-team`.",
  );
  await expect(panel).not.toContainText("payments");
  await shot?.("named-property-finding");

  // 3. Editing again makes those findings stale: a warning, the list collapsed, no "Blocking".
  await page.locator(".monaco-editor").first().click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.type("# more");
  await expect(panel).toContainText(
    /These findings are for revision \d+\. Save and validate to check the current text\./,
  );
  await expect(
    panel.getByRole("button", { name: "Show previous findings" }),
  ).toBeVisible();
  await expect(panel).not.toContainText("Blocking");
  await shot?.("stale-findings");
  await panel.getByRole("button", { name: "Show previous findings" }).click();
  await expect(panel).toContainText("Earlier errors");
  await expect(panel).not.toContainText("Blocking");

  // 4. Back to the original document, saved, so the caller can publish.
  await replaceBuffer(page, original);
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText("Draft saved.")).toBeVisible();
}
