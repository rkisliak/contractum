// One browser journey per role against the real stack (UI-16, Phase 13 success criteria 3), in
// order, each through the UI only:
//   1. Policy Manager (acme-policy-manager, Policy manager on Acme): the acme-baseline rule set,
//      its rules, versions, rollout and a dry run of its impact, the Dry runs page and the
//      templates area; no admin or audit navigation.
//   2. Editor (ledger-editor, Team editor on Ledger): creates a contract from the "Acme batch
//      table" template, is blocked at publish by a mandatory rule (the finding names the field,
//      /schema/0), fixes the draft and publishes it as the Latest.
//   3. Consumer (ledger-subscriber, Consumer on Ledger): finds that contract, reads its Latest
//      document and downloads it.
//   4. Org admin (acme-org-admin, Organization admin on Acme): sees those actions in the audit
//      explorer (the refused publish, the publish and the consumer's read) and grants, then
//      revokes, a role on Acme.
// The blocking rule set is per run: it lives on Ledger (no other journey publishes a template-made
// contract there), is mandatory and enforced now, and its one rule only looks at a table named
// like the template's ("table_name"), which must be described. (A rule keyed on the ODCS id
// through the `contract` binding fails the rule check's scale probe: the whole document is bound
// for every match.) Its mandatory flag is cleared, the journey contract soft-deleted and the
// grant revoked afterwards (also when an earlier run left them behind).
import { request, type APIRequestContext, type Page } from "@playwright/test";
import {
  changeStatusToActiveAndSave,
  expect,
  pasteIntoEditor,
  test,
} from "./fixtures";
import {
  adminPassword,
  escapeRegExp,
  expectCleanPage,
  findDemo,
  IssueCollector,
  visit,
  type Api,
  type Demo,
} from "./journey-helpers";
import {
  ACME_ORG_ADMIN,
  ACME_POLICY_MANAGER,
  ensurePersonas,
  LEDGER_EDITOR,
  LEDGER_READER,
  LEDGER_SUBSCRIBER,
  signInAs,
} from "./personas";

const GATEWAY_URL = process.env.DC_UI_E2E_SETUP_URL ?? "https://traefik";

const RUN = Date.now().toString(36);
const MARKER = `rolejourney${RUN}`;
const CONTRACT_PREFIX = "Role journey ";
const RULE_SET_PREFIX = "role-journey-";
const RULE_KEY = `table-described-${RUN}`;
const RULE_MESSAGE = "Describe every table of the contract.";
const GRANTEE = "acme-pipeline"; // the demo service account the Org admin grants a role to

test.use({
  actionTimeout: 15_000,
  navigationTimeout: 30_000,
});

// ---------- admin API (setup and cleanup only) ----------

type Admin = {
  ctx: APIRequestContext;
  call(
    method: "GET" | "POST" | "PUT" | "DELETE",
    path: string,
    options?: { json?: unknown; headers?: Record<string, string> },
  ): Promise<{ body: unknown; etag: string; status: number }>;
};

async function admin(): Promise<Admin> {
  const ctx = await request.newContext({
    baseURL: GATEWAY_URL,
    ignoreHTTPSErrors: true,
  });
  const login = await ctx.post("/api/v1/identity/auth/login", {
    data: { username: "admin", password: adminPassword() },
  });
  if (!login.ok()) throw new Error(`login ${String(login.status())}`);
  const { access_token } = (await login.json()) as { access_token: string };
  return {
    ctx,
    async call(
      method: "GET" | "POST" | "PUT" | "DELETE",
      path: string,
      options: { json?: unknown; headers?: Record<string, string> } = {},
    ) {
      const res = await ctx.fetch(`/api/v1/${path}`, {
        method,
        headers: {
          Authorization: `Bearer ${access_token}`,
          ...(options.json === undefined
            ? {}
            : { "Content-Type": "application/json" }),
          ...options.headers,
        },
        ...(options.json === undefined
          ? {}
          : { data: JSON.stringify(options.json) }),
      });
      if (!res.ok() && res.status() !== 404)
        throw new Error(
          `${method} ${path} ${String(res.status())}: ${await res.text()}`,
        );
      const text = await res.text();
      return {
        body: (text ? JSON.parse(text) : {}) as unknown,
        etag: res.headers()["etag"] ?? "",
        status: res.status(),
      };
    },
  };
}

type Items<T> = { items: T[]; next_cursor?: string | null };

/** Clears the mandatory flag of every per-run rule set, removes journey contracts and the grant. */
async function cleanup(api: Admin, demo: Demo): Promise<void> {
  const errors: string[] = [];
  const attempt = async (step: () => Promise<unknown>) => {
    try {
      await step();
    } catch (error) {
      errors.push(String(error));
    }
  };
  const sets = (
    await api.call(
      "GET",
      `validation/folders/${demo.ledger.id}/rule-sets?limit=100`,
    )
  ).body as Items<{ id: string; name: string; mandatory: boolean }>;
  for (const set of sets.items)
    if (set.name.startsWith(RULE_SET_PREFIX) && set.mandatory)
      await attempt(() =>
        api.call("PUT", `validation/rule-sets/${set.id}/mandatory`, {
          json: { mandatory: false },
        }),
      );
  const contracts = (
    await api.call(
      "GET",
      `contracts/contracts?folder_id=${demo.ledger.id}&limit=100`,
    )
  ).body as Items<{ id: string; name: string }>;
  for (const contract of contracts.items)
    if (contract.name.startsWith(CONTRACT_PREFIX))
      await attempt(() =>
        api.call("DELETE", `contracts/contracts/${contract.id}`),
      );
  const accounts = (
    await api.call("GET", "identity/service-accounts?limit=100")
  ).body as Items<{ id: string; name: string }>;
  const account = accounts.items.find((a) => a.name === GRANTEE);
  const users = (await api.call("GET", "identity/users?limit=100"))
    .body as Items<{
    id: string;
    username: string;
  }>;
  const reader = users.items.find((u) => u.username === LEDGER_READER.username);
  const grants = (
    await api.call("GET", `identity/folders/${demo.acme.id}/grants?limit=100`)
  ).body as Items<{ id: string; principal_id: string; role: string }>;
  for (const grant of grants.items) {
    const journeyGrant =
      (account !== undefined &&
        grant.principal_id.endsWith(account.id) &&
        grant.role === "consumer") ||
      (reader !== undefined &&
        grant.principal_id === `local:${reader.id}` &&
        grant.role === "org_reader");
    if (journeyGrant)
      await attempt(() => api.call("DELETE", `identity/grants/${grant.id}`));
  }
  if (errors.length > 0)
    throw new Error(`role journey cleanup: ${errors.join("; ")}`);
}

/** The per-run blocking rule set on Ledger: published, mandatory and enforced now. */
async function createBlockingRuleSet(api: Admin, demo: Demo): Promise<string> {
  const table = (described: boolean) => ({
    document: {
      schema: [
        described
          ? { name: "table_name", description: "Described." }
          : { name: "table_name" },
      ],
    },
  });
  const created = await api.call(
    "POST",
    `validation/folders/${demo.ledger.id}/rule-sets`,
    { json: { name: `${RULE_SET_PREFIX}${RUN}` } },
  );
  const id = (created.body as { id: string }).id;
  const saved = await api.call("PUT", `validation/rule-sets/${id}/draft`, {
    json: {
      description: "Role journey: tables are described (e2e, per run).",
      rules: [
        {
          key: RULE_KEY,
          description:
            "A table named table_name (the template's) is described.",
          target: '$.schema[?@.name == "table_name"]',
          condition: "has(this.description)",
          severity: "error",
          message: RULE_MESSAGE,
          fixtures: { pass: [table(true)], fail: [table(false)] },
        },
      ],
    },
    headers: { "If-Match": created.etag },
  });
  const check = (saved.body as { check?: { ok?: boolean } | null }).check;
  if (check?.ok === false)
    throw new Error(`rule set check failed: ${JSON.stringify(saved.body)}`);
  await api.call("POST", `validation/rule-sets/${id}/publish`, {
    headers: { "If-Match": saved.etag },
  });
  await api.call("PUT", `validation/rule-sets/${id}/mandatory`, {
    json: { mandatory: true },
  });
  await api.call("POST", `validation/rule-sets/${id}/versions/1/enforce-now`);
  return id;
}

// ---------- shared journey state ----------

let demo: Demo;
const contractName = `${CONTRACT_PREFIX}${RUN}`;
const odcsId = `urn:acme:payments:ledger:${MARKER}`;
let contractId = "";
let semver = "";

/**
 * The draft once fixed: the template's content with the table described and `status: active`
 * (a released version needs it), in the draft's own format (create-from-template stores what the
 * template build returns, JSON or YAML).
 */
function fixedDocument(version: string, json: boolean): string {
  const document = {
    kind: "DataContract",
    id: odcsId,
    apiVersion: "v3.2.0",
    name: contractName,
    version,
    status: "active",
    description: {
      purpose: "Starting point for a daily batch table at Acme.",
    },
    schema: [
      {
        name: "table_name",
        physicalType: "table",
        description: "One row per ledger entry, loaded daily.",
        properties: [{ name: "id", logicalType: "string" }],
      },
    ],
  };
  if (json) return `${JSON.stringify(document, null, 2)}\n`;
  return `kind: DataContract
id: ${odcsId}
apiVersion: v3.2.0
name: ${contractName}
version: "${version}"
status: active
description:
  purpose: Starting point for a daily batch table at Acme.
schema:
  - name: table_name
    physicalType: table
    description: One row per ledger entry, loaded daily.
    properties:
      - name: id
        logicalType: string
`;
}

async function primaryNavLabels(page: Page): Promise<string[]> {
  const nav = page.getByRole("navigation", { name: "Primary" });
  await expect(nav.getByRole("link").first()).toBeVisible();
  return (await nav.getByRole("link").allInnerTexts()).map((t) => t.trim());
}

const ADMIN_AREAS = [
  "Audit log",
  "Users",
  "Groups",
  "Service accounts",
  "Access tokens",
];

test.describe.serial("role journeys", () => {
  test.beforeAll(async () => {
    test.setTimeout(180_000);
    const api = await admin();
    try {
      const helpers = {
        get: async (path: string) => (await api.call("GET", path)).body,
        post: async (path: string, data?: unknown) =>
          (await api.call("POST", path, { json: data })).body,
        delete: async (path: string) => {
          await api.call("DELETE", path);
        },
        dispose: () => api.ctx.dispose(),
      };
      demo = await findDemo(helpers as Api);
      await ensurePersonas(helpers as Api);
      await cleanup(api, demo);
      await createBlockingRuleSet(api, demo);
    } finally {
      await api.ctx.dispose();
    }
  });

  test.afterAll(async () => {
    const api = await admin();
    try {
      await cleanup(api, demo);
    } finally {
      await api.ctx.dispose();
    }
  });

  test("Policy Manager reviews the baseline rules, their impact and the templates", async ({
    page,
  }) => {
    test.setTimeout(600_000);
    const issues = new IssueCollector(page);
    await signInAs(page, ACME_POLICY_MANAGER);
    await visit(page, issues, "/", {
      h1: `Welcome, ${ACME_POLICY_MANAGER.displayName}`,
    });
    const labels = await primaryNavLabels(page);
    for (const label of ["Templates", "Rule sets", "Dry runs", "Drift"])
      expect(labels, `nav shows ${label}`).toContain(label);
    for (const label of ADMIN_AREAS)
      expect(labels, `nav hides ${label}`).not.toContain(label);

    // The rule set, reached from the Rule sets list.
    await visit(page, issues, `/rule-sets?folder_id=${demo.acme.id}`, {
      h1: "Rule sets",
    });
    const main = page.getByRole("main");
    await main.getByRole("link", { name: "acme-baseline" }).first().click();
    await expect(
      page.getByRole("heading", { level: 1, name: "acme-baseline" }),
    ).toBeVisible();
    await expect(
      main.getByText("Mandatory", { exact: true }).first(),
    ).toBeVisible();
    // Its rules, in the draft editor a Policy Manager may edit.
    issues.reset();
    await page.getByRole("tab", { name: "Draft" }).click();
    for (const key of [
      "require-description",
      "owner-email-format",
      "domain-set",
    ])
      await expect(main.getByText(key, { exact: true }).first()).toBeVisible();
    await expectCleanPage(page, issues, "rule set draft", {
      h1: "acme-baseline",
    });
    for (const tab of ["Versions", "Rollout"]) {
      issues.reset();
      await page.getByRole("tab", { name: tab }).click();
      await expectCleanPage(page, issues, `rule set ${tab}`, {
        h1: "acme-baseline",
      });
    }
    // Impact: a dry run of the published version over Acme's contracts.
    issues.reset();
    await page.getByRole("tab", { name: "Impact" }).click();
    await main.getByRole("button", { name: "Run dry-run" }).click();
    await expect(main.getByLabel("Dry-run summary")).toBeVisible({
      timeout: 60_000,
    });
    await expectCleanPage(page, issues, "rule set impact", {
      h1: "acme-baseline",
    });

    // The Dry runs page, preselected on the rule set.
    await page
      .getByRole("navigation", { name: "Primary" })
      .getByRole("link", { name: "Dry runs", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { level: 1, name: "Dry runs" }),
    ).toBeVisible();
    await expectCleanPage(page, issues, "dry runs", { h1: "Dry runs" });

    // The templates area: the list, then the Acme template.
    issues.reset();
    await page
      .getByRole("navigation", { name: "Primary" })
      .getByRole("link", { name: "Templates", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { level: 1, name: "Templates" }),
    ).toBeVisible();
    await expectCleanPage(page, issues, "templates", { h1: "Templates" });
    issues.reset();
    await main.getByRole("link", { name: demo.template.name }).first().click();
    await expect(
      page.getByRole("heading", { level: 1, name: demo.template.name }),
    ).toBeVisible();
    await expectCleanPage(page, issues, "template", { h1: demo.template.name });
  });

  test("Editor creates from the template, is blocked by a mandatory rule, fixes and publishes", async ({
    page,
  }) => {
    test.setTimeout(600_000);
    const issues = new IssueCollector(page);
    await signInAs(page, LEDGER_EDITOR);

    // New contract from the Ledger folder's Contracts tab, from the Acme template.
    await visit(page, issues, `/folders/${demo.ledger.id}/contracts`, {
      h1: "Ledger",
    });
    const main = page.getByRole("main");
    await main.getByRole("button", { name: "New contract" }).first().click();
    await expect(
      page.getByRole("heading", { level: 1, name: "New contract" }),
    ).toBeVisible();
    await expect(page.getByLabel("Folder", { exact: true })).toHaveValue(
      demo.ledger.id,
    );
    await page.getByLabel("From a template").check();
    await page
      .getByLabel("Template", { exact: true })
      .selectOption(demo.template.id);
    await expect(page.getByLabel("Template preview")).toBeVisible();
    await page.getByLabel("Name").fill(contractName);
    await page.getByLabel("ODCS id").fill(odcsId);
    await expectCleanPage(page, issues, "new contract from template", {
      h1: "New contract",
    });
    await page.getByRole("button", { name: "Create contract" }).click();
    await expect(page).toHaveURL(/\/contracts\/[^/]+\/drafts\/[^/]+$/);
    contractId = new URL(page.url()).pathname.split("/")[2] ?? "";
    await expect(
      page.getByRole("heading", { level: 1, name: contractName }),
    ).toBeVisible();
    const editor = page.locator(".monaco-editor").first();
    await expect(editor).toBeVisible();
    await expectCleanPage(page, issues, "new draft", { h1: contractName });

    // The template says `status: draft` and a released version: the dialog keeps Publish disabled
    // until the status is changed to active and saved.
    await changeStatusToActiveAndSave(page);

    // Publish: the gate refuses it on the per-run mandatory rule, at the table's pointer.
    await page.getByRole("button", { name: "Publish", exact: true }).click();
    const publish = page.getByRole("dialog", { name: /^Publish .*\?$/ });
    await expect(publish).toBeVisible();
    const title =
      (await publish.getByRole("heading").first().textContent()) ?? "";
    semver = /^Publish (.+)\?$/.exec(title.trim())?.[1] ?? "";
    expect(semver, "declared version in the publish dialog").toMatch(
      /^\d+\.\d+\.\d+/,
    );
    issues.reset([422]);
    await publish.getByRole("button", { name: `Publish ${semver}` }).click();
    await expect(
      page.getByText(/^Publish blocked: \d+ blocking findings?$/),
    ).toBeVisible();
    const gate = page.getByRole("region", { name: "Publish gate result" });
    const finding = gate.getByRole("row").filter({ hasText: RULE_KEY });
    await expect(finding).toBeVisible();
    await expect(finding).toContainText(RULE_MESSAGE);
    await expect(finding).toContainText("/schema/0");
    await expect(
      finding.getByRole("button", { name: /^Go to line \d+$/ }),
    ).toBeVisible();
    await expectCleanPage(page, issues, "publish blocked", {
      h1: contractName,
    });
    await page.screenshot({
      path: "test-results/role-journeys/publish-blocked.png",
      fullPage: true,
    });

    // Fix the draft: describe the table, save.
    issues.reset();
    const json = (await editor.locator(".view-lines").innerText())
      .trimStart()
      .startsWith("{");
    await pasteIntoEditor(page, fixedDocument(semver, json));
    await expect(editor).toContainText("One row per ledger entry");
    await expect(page.getByText("Unsaved changes")).toBeVisible();
    await page.getByRole("button", { name: "Save draft" }).click();
    await expect(page.getByText("Saved", { exact: true })).toBeVisible();
    await expectCleanPage(page, issues, "fixed draft", { h1: contractName });

    // Publish again: it becomes the Latest.
    issues.reset();
    await page.getByRole("button", { name: "Publish", exact: true }).click();
    await page
      .getByRole("dialog", { name: `Publish ${semver}?` })
      .getByRole("button", { name: `Publish ${semver}` })
      .click();
    await expect(page.getByText(`Published ${semver} as Latest`)).toBeVisible();
    await expect(page).toHaveURL(/\/versions/);
    await expectCleanPage(page, issues, "published", { h1: contractName });
  });

  test("Consumer finds the published contract and downloads its Latest version", async ({
    page,
  }) => {
    test.setTimeout(300_000);
    expect(contractId, "the editor journey published a contract").not.toBe("");
    const issues = new IssueCollector(page);
    await signInAs(page, LEDGER_SUBSCRIBER);

    // Find it through the Contracts search.
    await visit(page, issues, "/contracts", { h1: "Contracts" });
    const main = page.getByRole("main");
    issues.reset();
    await main.getByLabel("Search contracts").fill(contractName);
    const link = main.getByRole("link", { name: contractName });
    await expect(link).toBeVisible();
    await link.click();
    await expect(
      page.getByRole("heading", { level: 1, name: contractName }),
    ).toBeVisible();
    await expect(main.getByText(semver).first()).toBeVisible();
    await expectCleanPage(page, issues, "contract", { h1: contractName });

    // The Document tab shows the Latest published document (a consumer reads nothing else).
    issues.reset();
    await page
      .getByRole("navigation", { name: "Contract sections" })
      .getByRole("link", { name: "Document" })
      .click();
    await expect(main.getByText(`(Latest ${semver}).`)).toBeVisible();
    await expect(page.locator(".monaco-editor").first()).toContainText(
      "One row per ledger entry",
    );
    await expectCleanPage(page, issues, "document", { h1: contractName });

    // Download it as YAML.
    const download = page.waitForEvent("download");
    await main.getByRole("button", { name: "Download YAML" }).click();
    const file = await download;
    expect(file.suggestedFilename()).toBe(
      `${odcsId.replace(/[^A-Za-z0-9._-]+/g, "-")}-${semver}.yaml`,
    );
    const path = await file.path();
    const { readFile } = await import("node:fs/promises");
    const text = await readFile(path, "utf8");
    expect(text).toContain(`id: ${odcsId}`);
    expect(text).toContain("description: One row per ledger entry");

    // The version page answers by SemVer too.
    await visit(page, issues, `/contracts/${contractId}/versions/${semver}`, {
      h1: contractName,
    });
  });

  test("Org admin sees the audit trail and manages access in Acme", async ({
    page,
  }) => {
    test.setTimeout(600_000);
    expect(contractId, "the editor journey published a contract").not.toBe("");
    const issues = new IssueCollector(page);
    await signInAs(page, ACME_ORG_ADMIN);
    await visit(page, issues, "/", {
      h1: `Welcome, ${ACME_ORG_ADMIN.displayName}`,
    });
    const labels = await primaryNavLabels(page);
    for (const label of ["Audit log", "Groups", "Service accounts"])
      expect(labels, `nav shows ${label}`).toContain(label);
    for (const label of ["Users", "Access tokens"])
      expect(labels, `nav hides ${label}`).not.toContain(label);

    // The audit explorer, filtered to the journey contract through the filter form.
    await page
      .getByRole("navigation", { name: "Primary" })
      .getByRole("link", { name: "Audit log", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { level: 1, name: "Audit log" }),
    ).toBeVisible();
    const filters = page.getByRole("search", { name: "Audit filters" });
    await filters.getByLabel("Object type").fill("contract");
    await filters.getByLabel("Object id").fill(contractId);
    await filters.getByRole("button", { name: "Apply filters" }).click();
    await expect(page).toHaveURL(new RegExp(`aggregate_id=${contractId}`));
    const main = page.getByRole("main");
    // Events reach the audit service through the outbox and the broker: reload until they land.
    const actions = [
      "contract.created",
      "contract.version.publish_refused",
      "contract.version_published",
      "read",
    ];
    await expect(async () => {
      await page.reload();
      const table = main.getByRole("table");
      await expect(table).toBeVisible({ timeout: 10_000 });
      for (const action of actions)
        await expect(
          table.getByText(action, { exact: true }).first(),
        ).toBeVisible({ timeout: 2_000 });
    }).toPass({ timeout: 120_000, intervals: [2_000, 5_000, 10_000] });
    issues.reset();
    await expectCleanPage(page, issues, "audit trail", { h1: "Audit log" });
    // The refused publish names the actor: the editor.
    const refused = main
      .getByRole("row")
      .filter({ hasText: "contract.version.publish_refused" })
      .first();
    await expect(refused).toContainText(/Lena Ledger|ledger-editor|local:/);
    await refused.getByRole("button").first().click();
    await expect(
      page.getByRole("dialog", { name: "Audit event" }),
    ).toBeVisible();
    await expectCleanPage(page, issues, "audit event", {
      h1: "Audit log",
      dialog: true,
    });
    await page.keyboard.press("Escape");
    await page.screenshot({
      path: "test-results/role-journeys/audit-trail.png",
      fullPage: true,
    });

    // Manage access on Acme: grant the demo service account a role, then revoke it.
    await visit(page, issues, `/folders/${demo.acme.id}/access`, {
      h1: "Acme",
    });
    await main.getByRole("button", { name: "Grant access" }).first().click();
    const grant = page.getByRole("dialog", { name: "Grant access" });
    await grant.getByLabel("Principal type").selectOption("service_account");
    await grant.getByLabel("Search service accounts").fill(GRANTEE);
    await grant
      .getByRole("option", { name: new RegExp(GRANTEE) })
      .first()
      .click();
    await grant.getByLabel("Role").selectOption("consumer");
    // The picker never asks for the user lists without the root-level user:read, so an Org admin
    // sees no 403 here: a refused read would be a gating bug, not something to allow.
    issues.reset();
    await grant.getByRole("button", { name: "Grant access" }).click();
    await expect(grant).toBeHidden();
    const revoke = main.getByRole("button", {
      name: new RegExp(`^Revoke Consumer from ${GRANTEE}`),
    });
    await expect(revoke).toBeVisible();
    await expectCleanPage(page, issues, "granted", { h1: "Acme" });
    issues.reset();
    await revoke.click();
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Revoke grant" })
      .click();
    await expect(page.getByRole("alertdialog")).toBeHidden();
    await expect(revoke).toHaveCount(0);
    await expectCleanPage(page, issues, "revoked", { h1: "Acme" });

    // A local user is not listed for an Org admin (no global user:read), but can be named by
    // exact username: grant Organization reader to the demo reader, then revoke it.
    await main.getByRole("button", { name: "Grant access" }).first().click();
    await expect(grant).toBeVisible();
    await grant.getByLabel("Search users").fill(LEDGER_READER.username);
    await grant.getByRole("option", { name: /Use local username/ }).click();
    await grant.getByLabel("Role").selectOption("org_reader");
    issues.reset();
    await grant.getByRole("button", { name: "Grant access" }).click();
    await expect(grant).toBeHidden();
    const revokeReader = main.getByRole("button", {
      name: new RegExp(
        `^Revoke Organization reader from .*${escapeRegExp(LEDGER_READER.displayName)}`,
      ),
    });
    await expect(revokeReader).toBeVisible();
    await expectCleanPage(page, issues, "granted by username", { h1: "Acme" });
    issues.reset();
    await revokeReader.click();
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Revoke grant" })
      .click();
    await expect(page.getByRole("alertdialog")).toBeHidden();
    await expect(revokeReader).toHaveCount(0);
  });
});
