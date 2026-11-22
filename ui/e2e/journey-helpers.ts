// Helpers for the whole-UI journey (`full-journey.spec.ts`): an API client for discovering demo
// data and seeding, a per-page issue collector (console errors, page errors, failed API calls) and
// `visit`, which loads a route and asserts the page rendered cleanly.
import {
  request,
  type APIRequestContext,
  type Page,
  type Response,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { expect, finishFiniteAnimations } from "./fixtures";

// Playwright's Node-side client ignores Chromium's host mapping, so it reaches the gateway by its
// compose name (see global-setup.ts).
const GATEWAY_URL = process.env.DC_UI_E2E_SETUP_URL ?? "https://traefik";

/** `value` as a literal inside a RegExp: every regex metacharacter, the backslash included, escaped. */
export function escapeRegExp(value: string): string {
  return value.replace(/[\\^$.*+?()[\]{}|/-]/g, "\\$&");
}

export function adminPassword(): string {
  const password = process.env.DC_UI_E2E_ADMIN_PASSWORD;
  if (!password)
    throw new Error("global-setup did not export DC_UI_E2E_ADMIN_PASSWORD");
  return password;
}

export async function signIn(page: Page): Promise<void> {
  await page.goto("/signin");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password", { exact: true }).fill(adminPassword());
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: /^Welcome, / }),
  ).toBeVisible();
}

export type Api = {
  get<T = Record<string, unknown>>(path: string): Promise<T>;
  post<T = Record<string, unknown>>(path: string, data?: unknown): Promise<T>;
  put<T = Record<string, unknown>>(path: string, data?: unknown): Promise<T>;
  /** PUT with `If-Match` (the settings sections). */
  putWithEtag<T = Record<string, unknown>>(
    path: string,
    data: unknown,
    etag: string,
  ): Promise<T>;
  /** PATCH; the status and the problem slug are returned, never thrown (a refusal is the point). */
  patchProblem(
    path: string,
    data: unknown,
  ): Promise<{ status: number; type: string | null }>;
  /** DELETE; a 404 (already gone) is not an error. */
  delete(path: string): Promise<void>;
  dispose(): Promise<void>;
};

export async function adminApi(): Promise<Api> {
  const ctx: APIRequestContext = await request.newContext({
    baseURL: GATEWAY_URL,
    ignoreHTTPSErrors: true,
  });
  const login = await ctx.post("/api/v1/identity/auth/login", {
    data: { username: "admin", password: adminPassword() },
  });
  if (!login.ok()) throw new Error(`login ${String(login.status())}`);
  const { access_token } = (await login.json()) as { access_token: string };
  const headers = { Authorization: `Bearer ${access_token}` };
  return {
    async get<T>(path: string) {
      const res = await ctx.get(`/api/v1/${path}`, { headers });
      if (!res.ok())
        throw new Error(
          `GET ${path} ${String(res.status())}: ${await res.text()}`,
        );
      return (await res.json()) as T;
    },
    async post<T>(path: string, data?: unknown) {
      const res = await ctx.post(`/api/v1/${path}`, {
        headers,
        ...(data === undefined ? {} : { data }),
      });
      if (!res.ok())
        throw new Error(
          `POST ${path} ${String(res.status())}: ${await res.text()}`,
        );
      return (await res.json()) as T;
    },
    async put<T>(path: string, data?: unknown) {
      const res = await ctx.put(`/api/v1/${path}`, {
        headers,
        ...(data === undefined ? {} : { data }),
      });
      if (!res.ok())
        throw new Error(
          `PUT ${path} ${String(res.status())}: ${await res.text()}`,
        );
      return (await res.json()) as T;
    },
    async putWithEtag<T>(path: string, data: unknown, etag: string) {
      const res = await ctx.put(`/api/v1/${path}`, {
        headers: { ...headers, "If-Match": etag },
        data,
      });
      if (!res.ok())
        throw new Error(
          `PUT ${path} ${String(res.status())}: ${await res.text()}`,
        );
      return (await res.json()) as T;
    },
    async patchProblem(path: string, data: unknown) {
      const res = await ctx.patch(`/api/v1/${path}`, { headers, data });
      const body = (await res.json().catch(() => ({}))) as { type?: string };
      return { status: res.status(), type: body.type ?? null };
    },
    async delete(path: string) {
      const res = await ctx.delete(`/api/v1/${path}`, { headers });
      if (!res.ok() && res.status() !== 404)
        throw new Error(
          `DELETE ${path} ${String(res.status())}: ${await res.text()}`,
        );
    },
    dispose: () => ctx.dispose(),
  };
}

/** Folders the journey creates under Acme > Payments are named with this prefix. */
export const JOURNEY_TEAM_PREFIX = "Journey ";

/**
 * Removes what earlier journey runs left behind, as far as the API allows: every active card in
 * a "Journey ..." team under `parentId` is archived (which ends its pins), every contract there
 * is soft-deleted, and the team folder is archived. Folders, cards and contracts are never hard
 * deleted by the platform, so they stay restorable and in the audit trail.
 */
export async function cleanupJourneyTeams(
  api: Api,
  parentId: string,
): Promise<number> {
  type Page<T> = { items: T[]; next_cursor?: string | null };
  type Folder = { id: string; parent_id: string | null; display_name: string };
  const folders: Folder[] = [];
  let cursor: string | null | undefined = null;
  do {
    const page: Page<Folder> = await api.get<Page<Folder>>(
      `identity/folders?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    );
    folders.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  const teams = folders.filter(
    (f) =>
      f.parent_id === parentId &&
      f.display_name.startsWith(JOURNEY_TEAM_PREFIX),
  );
  // Each step is attempted even when an earlier one failed (a refused contract delete must not
  // leave the team active); the errors are reported together at the end.
  const errors: string[] = [];
  const attempt = async (step: () => Promise<unknown>) => {
    try {
      await step();
    } catch (error) {
      errors.push(String(error));
    }
  };
  for (const team of teams) {
    await attempt(async () => {
      const cards = await api.get<
        Page<{ id: string; archived_at?: string | null }>
      >(`contracts/folders/${team.id}/cards`);
      for (const card of cards.items) {
        if (!card.archived_at)
          await attempt(() => api.post(`contracts/cards/${card.id}/archive`));
      }
    });
    await attempt(async () => {
      const contracts = await api.get<Page<{ id: string }>>(
        `contracts/contracts?folder_id=${team.id}&limit=100`,
      );
      for (const contract of contracts.items)
        await attempt(() => api.delete(`contracts/contracts/${contract.id}`));
    });
    await attempt(() => api.post(`identity/folders/${team.id}/archive`));
  }
  if (errors.length > 0)
    throw new Error(`journey cleanup: ${errors.join("; ")}`);
  return teams.length;
}

type Issue = string;

/** Collects console errors, uncaught page errors and failed API responses for the current page. */
export class IssueCollector {
  private issues: Issue[] = [];
  /** HTTP statuses of API responses that the current page expects (for example a 404 probe). */
  allowStatuses = new Set<number>();
  private ignore: RegExp[] = [];

  constructor(page: Page) {
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      const text = message.text();
      // A failed fetch is reported by the response listener with its URL and status.
      if (/^Failed to load resource/.test(text)) return;
      if (this.ignore.some((re) => re.test(text))) return;
      this.issues.push(`console error: ${text}`);
    });
    page.on("pageerror", (error) => {
      this.issues.push(`page error: ${error.message}`);
    });
    page.on("response", (response: Response) => {
      const url = response.url();
      if (!url.includes("/api/v1/")) return;
      const status = response.status();
      if (status < 400) return;
      // The refresh probe answers 401 when no session cookie exists (sign-in page).
      if (url.endsWith("/identity/auth/refresh") && status === 401) return;
      if (this.allowStatuses.has(status)) return;
      const path = url.replace(/^https?:\/\/[^/]+/, "");
      this.issues.push(
        `${response.request().method()} ${path} answered ${String(status)}`,
      );
    });
  }

  reset(allow: number[] = [], ignore: RegExp[] = []): void {
    this.issues = [];
    this.allowStatuses = new Set(allow);
    this.ignore = ignore;
  }

  take(): Issue[] {
    const out = this.issues;
    this.issues = [];
    return out;
  }
}

export type VisitOptions = {
  /** The expected h1 (exact string or pattern). */
  h1: string | RegExp;
  /** API statuses the page is allowed to see (for example 404 for a probe). */
  allow?: number[];
  /** A ProblemAlert is expected on this page. */
  expectProblem?: boolean;
  /** A modal dialog, alert dialog or drawer is open: check it instead of the (now inert) h1. */
  dialog?: boolean;
  /** Skip the axe scan (for pages already scanned in the same state). */
  axe?: boolean;
};

/** Asserts the page currently shown rendered cleanly (h1, no problem, no issues, axe). */
export async function expectCleanPage(
  page: Page,
  issues: IssueCollector,
  label: string,
  options: VisitOptions,
): Promise<void> {
  await expect(
    options.dialog
      ? page.getByRole("dialog").or(page.getByRole("alertdialog")).first()
      : page.getByRole("heading", { level: 1, name: options.h1 }).first(),
    `${label}: h1`,
  ).toBeVisible({ timeout: 15_000 });
  await page.waitForLoadState("networkidle").catch(() => undefined);
  // Wait for loading skeletons and spinners to settle.
  await expect(page.locator("[aria-busy='true']"))
    .toHaveCount(0, { timeout: 10_000 })
    .catch(() => undefined);
  await expect
    .soft(
      page.getByText("Something went wrong!"),
      `${label}: router error boundary`,
    )
    .toHaveCount(0);
  if (!options.expectProblem) {
    await expect
      .soft(
        page.getByText("Request ID", { exact: true }),
        `${label}: a ProblemAlert is shown`,
      )
      .toHaveCount(0);
  }
  expect
    .soft(issues.take(), `${label}: console, page or API errors`)
    .toEqual([]);
  if (options.axe !== false) await softAxe(page, label);
}

/** Loads a route through a full navigation and checks it with `expectCleanPage`. */
export async function visit(
  page: Page,
  issues: IssueCollector,
  path: string,
  options: VisitOptions,
): Promise<void> {
  issues.reset(options.allow ?? []);
  await page.goto(path);
  await expectCleanPage(page, issues, path, options);
}

const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

/** The fixtures' axe gate as a soft assertion, so one journey reports every page at once. */
export async function softAxe(page: Page, label: string): Promise<void> {
  await finishFiniteAnimations(page);
  const results = await new AxeBuilder({ page }).withTags(AXE_TAGS).analyze();
  const report = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map(
      (v) =>
        `${v.id} (${v.impact ?? "unknown"}): ${v.nodes
          .map((node) => node.target.join(" "))
          .slice(0, 5)
          .join(", ")}`,
    );
  expect
    .soft(report, `${label}: serious or critical axe violations`)
    .toEqual([]);
}

// ---------- demo data ----------

export type Folder = {
  id: string;
  kind: string;
  path: string;
  display_name: string;
  parent_id: string | null;
};
type Page_<T> = { items: T[]; next_cursor?: string | null };

export type Demo = {
  root: Folder;
  acme: Folder;
  payments: Folder;
  ledger: Folder;
  fraud: Folder;
  orders: { id: string; name: string };
  draftId: string | null;
  ruleSet: { id: string; name: string };
  template: { id: string; name: string };
  card: { id: string; name: string } | null;
  userId: string;
  groupId: string | null;
  serviceAccountId: string | null;
  entraUserId: string | null;
  entraGroupId: string | null;
  entraSpId: string | null;
};

/** The demo dataset (see full-journey.spec.ts), found through the API. */
export async function findDemo(api: Api): Promise<Demo> {
  const folders = await api.get<Page_<Folder>>("identity/folders?limit=100");
  const byPath = (path: string) => {
    const folder = folders.items.find((f) => f.path === path);
    if (!folder) throw new Error(`demo folder ${path} is missing`);
    return folder;
  };
  const root = folders.items.find((f) => f.kind === "root");
  if (!root) throw new Error("no ROOT folder");
  const ledger = byPath("acme/payments/ledger");
  const contracts = await api.get<Page_<{ id: string; name: string }>>(
    `contracts/contracts?folder_id=${ledger.id}&odcs_id=urn:acme:ledger:orders`,
  );
  const orders = contracts.items[0];
  if (!orders) throw new Error("demo contract Orders is missing");
  const drafts = await api.get<Page_<{ id: string }>>(
    `contracts/contracts/${orders.id}/drafts`,
  );
  const acme = byPath("acme");
  const ruleSets = await api.get<Page_<{ id: string; name: string }>>(
    `validation/folders/${acme.id}/rule-sets`,
  );
  const ruleSet = ruleSets.items.find((r) => r.name === "acme-baseline");
  if (!ruleSet) throw new Error("demo rule set acme-baseline is missing");
  const templates = await api.get<Page_<{ id: string; name: string }>>(
    `contracts/templates?folder_id=${acme.id}`,
  );
  const template = templates.items.find((t) => t.name === "Acme batch table");
  if (!template) throw new Error("demo template is missing");
  const fraud = byPath("acme/payments/fraud");
  const cards = await api.get<Page_<{ id: string; name: string }>>(
    `contracts/folders/${fraud.id}/cards`,
  );
  const users = await api.get<Page_<{ id: string; username: string }>>(
    "identity/users?limit=100",
  );
  const groups = await api.get<Page_<{ id: string }>>("identity/groups");
  const sas = await api.get<Page_<{ id: string }>>("identity/service-accounts");
  const eu = await api.get<Page_<{ id: string }>>("identity/entra/users");
  const eg = await api.get<Page_<{ id: string }>>("identity/entra/groups");
  const esp = await api.get<Page_<{ id: string }>>(
    "identity/entra/service-principals",
  );
  return {
    root,
    acme,
    payments: byPath("acme/payments"),
    ledger,
    fraud,
    orders,
    draftId: drafts.items[0]?.id ?? null,
    ruleSet,
    template,
    card: cards.items[0] ?? null,
    userId:
      users.items.find((u) => u.username === "admin")?.id ??
      users.items[0]?.id ??
      "",
    groupId: groups.items[0]?.id ?? null,
    serviceAccountId: sas.items[0]?.id ?? null,
    entraUserId: eu.items[0]?.id ?? null,
    entraGroupId: eg.items[0]?.id ?? null,
    entraSpId: esp.items[0]?.id ?? null,
  };
}
