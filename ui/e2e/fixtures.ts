import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, test as base, type Page } from "@playwright/test";

// Shared Playwright fixtures for every UI journey (D-UI-08-29, D-UI-08-32).
//
// `test` adds an automatic CSP-violation collector: any `securitypolicyviolation` event or console
// message about Content Security Policy fails the test that caused it (Pitfall 11). Journeys also
// get `expectNoSeriousA11yViolations` (the UI-SPEC section 8 axe gate) and
// `expectNoHorizontalOverflow` (the reflow check).
//
// A second automatic fixture records every `/api/v1/` response the page receives (method, path
// without the query string, status, spec file, test title and project; never a header or body) to
// `test-results/api-calls/<worker>.jsonl`. `scripts/operation_coverage.py` (the full gate) reads it
// to show which OpenAPI operations each UI journey exercises.

const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

declare global {
  interface Window {
    __dcCspViolations?: string[];
  }
}

export const test = base.extend<{ cspViolations: string[]; apiCalls: null }>({
  apiCalls: [
    async ({ page }, use, testInfo) => {
      const dir = join(testInfo.project.outputDir, "api-calls");
      mkdirSync(dir, { recursive: true });
      const file = join(dir, `${String(testInfo.workerIndex)}.jsonl`);
      page.on("response", (response) => {
        const url = new URL(response.url());
        if (!url.pathname.startsWith("/api/v1/")) return;
        const line = JSON.stringify({
          method: response.request().method(),
          path: url.pathname,
          status: response.status(),
          spec: testInfo.file.split("/").pop(),
          test: testInfo.title,
          project: testInfo.project.name,
        });
        appendFileSync(file, `${line}\n`);
      });
      await use(null);
    },
    { auto: true },
  ],
  cspViolations: [
    async ({ page }, use) => {
      const violations: string[] = [];
      await page.addInitScript(() => {
        window.__dcCspViolations = [];
        document.addEventListener("securitypolicyviolation", (event) => {
          window.__dcCspViolations?.push(
            `${event.violatedDirective} blocked ${event.blockedURI || "inline"}`,
          );
        });
      });
      page.on("console", (message) => {
        if (/content security policy/i.test(message.text())) {
          violations.push(`console: ${message.text()}`);
        }
      });
      await use(violations);
      // Collect what the page recorded (a navigation resets it, so every goto is read by the
      // console listener too).
      const inPage = await page
        .evaluate(() => window.__dcCspViolations ?? [])
        .catch((): string[] => []);
      violations.push(...inPage);
      expect(violations, "CSP violations recorded on the page").toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };

/**
 * Waits for every finite animation on the page (dialog fade-in, drawer slide) to finish. Run it
 * before an axe scan: mid-fade text is partly transparent and axe measures it as low contrast,
 * which is not what the user reads. Infinite animations (spinners, a blinking cursor) never finish
 * and are left running.
 */
export async function finishFiniteAnimations(page: Page): Promise<void> {
  await page.evaluate(() =>
    Promise.all(
      document
        .getAnimations()
        .filter(
          (animation) => animation.effect?.getTiming().iterations !== Infinity,
        )
        .map((animation) => animation.finished.catch(() => undefined)),
    ),
  );
}

/** Fails on any serious or critical axe violation (WCAG 2.2 AA tag set). */
export async function expectNoSeriousA11yViolations(page: Page): Promise<void> {
  await finishFiniteAnimations(page);
  const results = await new AxeBuilder({ page }).withTags(AXE_TAGS).analyze();
  const blocking = results.violations.filter(
    (violation) =>
      violation.impact === "serious" || violation.impact === "critical",
  );
  const report = blocking.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? "unknown"}): ${violation.nodes
        .map((node) => node.target.join(" "))
        .join(", ")}`,
  );
  expect(report, "serious or critical axe violations").toEqual([]);
}

/**
 * Replaces the whole content of the first Monaco editor on the page with `text` the way a paste
 * does, and waits until the editor holds exactly `text`. It selects all and sends the editor's
 * focused input a paste event that carries `text` as its clipboard data: Monaco takes its paste
 * path and inserts the text verbatim. Typed input (`keyboard.insertText`) would get Monaco's
 * type-time auto-indent and auto-closing brackets, which corrupt a multi-line document. The
 * browser-wide system clipboard, which headless Chromium shares between tests, is not used.
 *
 * Monaco applies a paste asynchronously and drops it when the selection or the text changes
 * first, so nothing may touch the editor (not even Ctrl+Home) until the text has landed: the
 * helper only returns once every rendered line equals its line of `text`.
 */
export async function pasteIntoEditor(page: Page, text: string): Promise<void> {
  const editor = page.locator(".monaco-editor").first();
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.evaluate((value) => {
    const input = document.activeElement;
    if (!input?.closest(".monaco-editor")) {
      throw new Error("pasteIntoEditor: the Monaco editor does not have focus");
    }
    const clipboardData = new DataTransfer();
    clipboardData.setData("text/plain", value);
    input.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData,
        bubbles: true,
        cancelable: true,
      }),
    );
  }, text);
  // Monaco renders the lines in view. The line-number gutter tells which line each rendered row
  // is; with word wrap a line spans several rows, only the first one numbered, and a continuation
  // row starts with the wrap indent, which is not part of the text.
  const expected = text.split("\n");
  await expect
    .poll(async () => {
      const rendered = await editor.evaluate((root) => {
        const top = (node: Element | null) =>
          Number.parseFloat((node as HTMLElement | null)?.style.top ?? "");
        const numbers = new Map(
          [...root.querySelectorAll(".margin-view-overlays .line-numbers")]
            .filter((node) => node.textContent.trim() !== "")
            .map((node): [number, number] => [
              top(node.parentElement),
              Number(node.textContent),
            ]),
        );
        const rows = [...root.querySelectorAll(".view-lines .view-line")]
          .map((node) => ({
            top: top(node),
            text: node.textContent.replaceAll("\u00a0", " "),
          }))
          .sort((a, b) => a.top - b.top);
        const lines: { number: number; text: string }[] = [];
        for (const row of rows) {
          const number = numbers.get(row.top);
          // A wrapped line cut off at the top of the view is skipped.
          if (number === undefined && lines.length === 0) continue;
          const previous = number === undefined ? lines.pop() : undefined;
          lines.push(
            previous === undefined
              ? { number: number ?? 0, text: row.text }
              : { ...previous, text: previous.text + row.text.trimStart() },
          );
        }
        return lines;
      });
      if (rendered.length === 0) return ["no line is rendered"];
      return rendered.flatMap(({ number, text: line }) =>
        line === expected[number - 1]
          ? []
          : [
              `line ${String(number)}: ${JSON.stringify(line)} is not ${JSON.stringify(expected[number - 1])}`,
            ],
      );
    }, "the editor holds exactly the pasted text")
    .toEqual([]);
}

/**
 * The publish dialog of a released version whose document does not say `status: active`:
 * Publish stays disabled, "Change status to active" rewrites the status value in the editor and
 * closes the dialog, and the draft is saved so it can be published.
 */
export async function changeStatusToActiveAndSave(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: /^Publish .*\?$/ });
  await expect(dialog).toContainText("A released version needs status: active");
  await expect(
    dialog.getByRole("button", { name: /^Publish \S+$/ }),
  ).toBeDisabled();
  await dialog.getByRole("button", { name: "Change status to active" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText("Unsaved changes")).toBeVisible();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText("Draft saved.")).toBeVisible();
}

/** Fails when the page scrolls horizontally at the page's current viewport width. */
export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(
    scrollWidth,
    "document scrollWidth vs clientWidth",
  ).toBeLessThanOrEqual(clientWidth);
}
