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
 * Replaces the whole content of the first Monaco editor on the page with `text`: select all, then
 * insert the text in one input event, as a paste does (no per-key auto-indent or auto-closing). It
 * does not go through the browser-wide system clipboard, which headless Chromium shares between
 * tests and which stopped delivering pastes after earlier specs ran.
 */
export async function pasteIntoEditor(page: Page, text: string): Promise<void> {
  await page.locator(".monaco-editor").first().click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.insertText(text);
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
