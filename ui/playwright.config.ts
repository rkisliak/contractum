import { defineConfig, devices } from "@playwright/test";

// The runner container reaches the gateway as traefik:443; Chromium is told to resolve the
// browser-visible origin (the one the Entra issuer is registered under) to it, so the page origin
// stays https://localhost:8443 (A-11). One worker and no retries: the journeys share one stack and
// the admin account.
export default defineConfig({
  testDir: "e2e",
  workers: 1,
  retries: 0,
  fullyParallel: false,
  reporter: [["list"]],
  outputDir: "test-results",
  // Supplies DC_UI_E2E_ADMIN_PASSWORD (from /dev-out, bootstrapping when stale).
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: process.env.DC_UI_E2E_BASE_URL ?? "https://localhost:8443",
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    launchOptions: {
      args: ["--host-resolver-rules=MAP localhost:8443 traefik:443"],
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      // The volume audit has its own projects and needs the volume seed.
      testIgnore: /volume\.(spec|setup)\.ts$/,
    },
    // The enterprise design system has an automatic dark theme (prefers-color-scheme): the
    // read-only full journey (h1, no errors, axe) and the design screenshots run again in dark.
    {
      name: "chromium-dark",
      use: { ...devices["Desktop Chrome"], colorScheme: "dark" },
      testMatch: /(full-journey|design-screens|layout)\.spec\.ts$/,
      grep: /every area renders cleanly|design screenshots|layout audit/,
    },
    // The high-volume rendering audit (quick 261120-6i0): `volume-setup` seeds the compose test
    // stack through the admin API (idempotent, minutes on a cold stack); `volume` and
    // `volume-dark` walk the seeded stack. Run with project flags, not file paths:
    //   bash scripts/e2e-ui.sh --project=volume-setup
    //   bash scripts/e2e-ui.sh --project=volume --project=volume-dark
    {
      name: "volume-setup",
      testMatch: /volume\.setup\.ts$/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "volume",
      testMatch: /volume\.spec\.ts$/,
      dependencies: ["volume-setup"],
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "volume-dark",
      testMatch: /volume\.spec\.ts$/,
      dependencies: ["volume-setup"],
      use: { ...devices["Desktop Chrome"], colorScheme: "dark" },
    },
  ],
});
