import { chmod, readFile, rm, writeFile } from "node:fs/promises";
import {
  chromium,
  request,
  type APIRequestContext,
  type FullConfig,
} from "@playwright/test";
import { ensureDemoData } from "./demo-seed";

// Supplies the admin password to every journey as DC_UI_E2E_ADMIN_PASSWORD, exactly like the
// Python e2e fixture `tests/e2e/conftest.py::admin_password` (research A-12): use the password in
// /dev-out/admin_password when it still logs in, otherwise sign in with the one-time bootstrap
// secret through the browser, complete the /signin/rotate screen with a random password, and
// write it back (0600). DC_UI_E2E_ROTATED=1 tells the smoke spec the rotation journey ran (only
// on a cold stack: the one-time secret is consumed after that).
//
// Playwright's Node-side request client does not honour Chromium's --host-resolver-rules, so it
// reaches the gateway by its compose name (a host in the dev certificate) instead of
// https://localhost:8443.

const LOGIN_PATH = "/api/v1/identity/auth/login";
const ADMIN_USERNAME = "admin";

const GATEWAY_URL = process.env.DC_UI_E2E_SETUP_URL ?? "https://traefik";
const DEV_OUT = process.env.DC_UI_E2E_DEV_OUT ?? "/dev-out";
const SECRETS = process.env.DC_UI_E2E_SECRETS ?? "/run/secrets";

type TokenBody = { access_token: string };

async function login(api: APIRequestContext, password: string) {
  return api.post(LOGIN_PATH, {
    data: { username: ADMIN_USERNAME, password },
  });
}

async function readText(path: string): Promise<string | null> {
  try {
    return (await readFile(path, "utf-8")).trim();
  } catch {
    return null;
  }
}

function randomPassword(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  return Buffer.from(bytes).toString("base64url");
}

// Seeds the demo dataset (demo-seed.ts), then admits the Entra personas. The Entra persona Alice (oid a11ce000-...) is a member of Data Eng in the fake directory; the
// platform admits her once that group is registered (409 already-registered is fine).
const ENTRA_DATA_ENG_GROUP = "9a0f0000-0000-4000-8000-0000000000a1";

async function admitEntraPersonas(
  api: APIRequestContext,
  password: string,
): Promise<void> {
  const res = await login(api, password);
  if (!res.ok()) return;
  const { access_token } = (await res.json()) as TokenBody;
  const headers = { Authorization: `Bearer ${access_token}` };
  // The journeys read a fixed demo dataset; a cold stack has none yet.
  await ensureDemoData(api, access_token);
  const folders = await api.get("/api/v1/identity/folders?limit=100", {
    headers,
  });
  const root = (
    (await folders.json()) as { items: { id: string; kind: string }[] }
  ).items.find((folder) => folder.kind === "root");
  if (!root) return;
  const registered = await api.post("/api/v1/identity/entra/groups", {
    headers,
    data: { owner_folder_id: root.id, object_id: ENTRA_DATA_ENG_GROUP },
  });
  if (!registered.ok() && registered.status() !== 409) {
    throw new Error(
      `Registering the Data Eng group answered ${String(registered.status())}`,
    );
  }
}

// The cold-stack bootstrap journey (UI-SPEC 7.3): sign in with the one-time secret, get sent to
// /signin/rotate, set the new password and land signed in on the welcome heading.
async function rotateThroughBrowser(
  config: FullConfig,
  secret: string,
  newPassword: string,
): Promise<void> {
  const use = config.projects[0]?.use ?? {};
  const browser = await chromium.launch(use.launchOptions);
  try {
    const page = await browser.newPage({
      baseURL: use.baseURL ?? "https://localhost:8443",
      ignoreHTTPSErrors: true,
    });
    await page.goto("/signin");
    await page.getByLabel("Username").fill(ADMIN_USERNAME);
    await page.getByLabel("Password", { exact: true }).fill(secret);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page
      .getByRole("heading", { level: 1, name: "Set a new password" })
      .waitFor();
    await page.getByLabel("New password", { exact: true }).fill(newPassword);
    await page.getByLabel("Confirm new password").fill(newPassword);
    await page.getByRole("button", { name: "Set new password" }).click();
    await page.getByRole("heading", { level: 1, name: /^Welcome,/ }).waitFor();
  } finally {
    await browser.close();
  }
}

export default async function globalSetup(config: FullConfig): Promise<void> {
  const api = await request.newContext({
    baseURL: GATEWAY_URL,
    ignoreHTTPSErrors: true,
  });
  try {
    const storedPath = `${DEV_OUT}/admin_password`;
    const stored = await readText(storedPath);
    if (stored) {
      if ((await login(api, stored)).ok()) {
        process.env.DC_UI_E2E_ADMIN_PASSWORD = stored;
        await admitEntraPersonas(api, stored);
        return;
      }
      await rm(storedPath, { force: true });
    }

    const secret = await readText(`${SECRETS}/bootstrap_admin_secret`);
    if (!secret) {
      throw new Error(
        `No usable admin password: ${storedPath} did not log in and ${SECRETS}/bootstrap_admin_secret is missing`,
      );
    }
    const newPassword = randomPassword();
    await rotateThroughBrowser(config, secret, newPassword);
    if (!(await login(api, newPassword)).ok()) {
      throw new Error("The password set on the rotate screen does not log in");
    }
    await writeFile(storedPath, newPassword, "utf-8");
    await chmod(storedPath, 0o600);
    process.env.DC_UI_E2E_ADMIN_PASSWORD = newPassword;
    process.env.DC_UI_E2E_ROTATED = "1";
    await admitEntraPersonas(api, newPassword);
  } finally {
    await api.dispose();
  }
}
