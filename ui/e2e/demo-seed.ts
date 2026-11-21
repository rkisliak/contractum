// The demo dataset the journeys read (journey-helpers.ts `findDemo`), created through the API as
// admin when it is missing, so the UI e2e suite also runs on a cold stack (`CI_FULL=1
// scripts/ci.sh` wipes the volumes first). Every step looks before it creates, so a second run on
// the same stack changes nothing:
//   Acme > Payments > Ledger and Fraud (folders)
//   Ledger: contract "Orders" (urn:acme:ledger:orders) 1.0.0 and 1.1.0 published, a 1.2.0 draft
//   Acme: rule set acme-baseline (three rules as in the hand-made demo, version 1 published,
//         mandatory, so it warns first) and the published template "Acme batch table"
//   Fraud: card demo-analytics-team pinned to Orders 1.0.0
//   Identities: local user demo-alice in the Acme group acme-data-engineers, the Acme service
//   account acme-pipeline, and the fake directory's Entra user Alice and service principal
//   Demo pipeline registered at ROOT (the Data Eng group is registered by global-setup.ts)
import type { APIRequestContext } from "@playwright/test";
import { Client, type Page_ } from "./seed-client";

type Folder = { id: string; path: string; kind: string };
type Draft = { id: string; etag: string };

const ORDERS_ODCS_ID = "urn:acme:ledger:orders";

function ordersYaml(version: string): string {
  return `kind: DataContract
id: ${ORDERS_ODCS_ID}
apiVersion: v3.2.0
name: Orders
version: "${version}"
status: active
domain: payments
description:
  purpose: Orders booked by the Ledger team, one row per order.
schema:
  - name: orders
    physicalType: table
    description: Booked orders.
    properties:
      - name: order_id
        logicalType: string
        description: Unique order identifier.
      - name: amount
        logicalType: number
        description: Order total.
`;
}

const TEMPLATE_YAML = `kind: DataContract
id: urn:acme:template:batch-table
apiVersion: v3.2.0
name: Acme batch table
version: "1.0.0"
status: draft
description:
  purpose: Starting point for a daily batch table at Acme.
schema:
  - name: table_name
    physicalType: table
    properties:
      - name: id
        logicalType: string
`;

const BASELINE_RULES = [
  {
    key: "require-description",
    description: "Every contract explains its purpose.",
    target: "$",
    condition: "has(this.description)",
    severity: "error",
    message: "Add a description to the contract.",
    fixtures: {
      pass: [{ document: { description: { purpose: "x" } } }],
      fail: [{ document: { name: "no description" } }],
    },
  },
  {
    key: "owner-email-format",
    description: "Support channels are mail or web links.",
    target: "$",
    condition:
      '!has(this.support) || this.support.all(s, !has(s.url) || s.url.startsWith("mailto:") || s.url.startsWith("https://"))',
    severity: "warning",
    message: "Use a mailto: or https:// link for support.",
    fixtures: {
      pass: [{ document: { support: [{ url: "mailto:team@example.com" }] } }],
      fail: [{ document: { support: [{ url: "ftp://example.com" }] } }],
    },
  },
  {
    key: "domain-set",
    description: "A contract names its business domain.",
    target: "$",
    condition: "has(this.domain)",
    severity: "info",
    message: "Set the domain of the contract.",
    fixtures: {
      pass: [{ document: { domain: "payments" } }],
      fail: [{ document: { name: "no domain" } }],
    },
  },
];

async function ensureFolders(api: Client): Promise<Record<string, Folder>> {
  const wanted: [path: string, parent: string | null, name: string][] = [
    ["acme", null, "Acme"],
    ["acme/payments", "acme", "Payments"],
    ["acme/payments/ledger", "acme/payments", "Ledger"],
    ["acme/payments/fraud", "acme/payments", "Fraud"],
  ];
  const folders = await api.all<Folder>("identity/folders");
  const root = folders.find((f) => f.kind === "root");
  if (!root) throw new Error("demo seed: no ROOT folder");
  const byPath: Record<string, Folder> = { "": root };
  for (const [path, parent, name] of wanted) {
    const found = folders.find((f) => f.path === path);
    if (found) {
      byPath[path] = found;
      continue;
    }
    const parentId = parent === null ? root.id : byPath[parent]?.id;
    if (!parentId) throw new Error(`demo seed: no parent for ${path}`);
    const created = await api.post("identity/folders", {
      json: {
        parent_id: parentId,
        slug: path.split("/").at(-1),
        display_name: name,
      },
    });
    byPath[path] = { ...(created.body as Folder), path };
  }
  return byPath;
}

async function publishDraft(
  api: Client,
  contractId: string,
  draft: Draft,
): Promise<void> {
  await api.post(
    `contracts/contracts/${contractId}/drafts/${draft.id}/publish`,
    {
      headers: { "If-Match": draft.etag },
    },
  );
}

async function ensureOrders(api: Client, ledgerId: string): Promise<string> {
  const listed = await api.get<Page_<{ id: string }>>(
    `contracts/contracts?folder_id=${ledgerId}&odcs_id=${ORDERS_ODCS_ID}`,
  );
  const existing = listed.items[0];
  if (existing) return existing.id;
  const created = (
    await api.post(`contracts/folders/${ledgerId}/contracts`, {
      yaml: ordersYaml("1.0.0"),
    })
  ).body as { contract: { id: string }; draft: Draft };
  const contractId = created.contract.id;
  await publishDraft(api, contractId, created.draft);
  const next = async (from: string) =>
    (
      (
        await api.post(`contracts/contracts/${contractId}/drafts`, {
          json: { from_version: from, bump: "minor" },
        })
      ).body as { draft: Draft }
    ).draft;
  await publishDraft(api, contractId, await next("1.0.0"));
  await next("1.1.0"); // left as the 1.2.0 draft the editor screens open
  return contractId;
}

async function ensureRuleSet(api: Client, acmeId: string): Promise<void> {
  const sets = await api.all<{ name: string }>(
    `validation/folders/${acmeId}/rule-sets`,
  );
  if (sets.some((s) => s.name === "acme-baseline")) return;
  const created = await api.post(`validation/folders/${acmeId}/rule-sets`, {
    json: { name: "acme-baseline" },
  });
  const { id } = created.body as { id: string };
  const saved = await api.put(`validation/rule-sets/${id}/draft`, {
    json: { description: "Acme baseline rules", rules: BASELINE_RULES },
    headers: { "If-Match": created.etag },
  });
  await api.post(`validation/rule-sets/${id}/publish`, {
    headers: { "If-Match": saved.etag },
  });
  await api.put(`validation/rule-sets/${id}/mandatory`, {
    json: { mandatory: true },
  });
}

async function ensureTemplate(api: Client, acmeId: string): Promise<void> {
  const templates = await api.all<{ name: string }>(
    `contracts/templates?folder_id=${acmeId}`,
  );
  if (templates.some((t) => t.name === "Acme batch table")) return;
  const created = await api.post(
    `contracts/folders/${acmeId}/templates?name=${encodeURIComponent("Acme batch table")}`,
    { yaml: TEMPLATE_YAML },
  );
  const { template } = created.body as { template: { id: string } };
  await api.post(`contracts/templates/${template.id}/publish`);
}

async function ensureCard(
  api: Client,
  fraudId: string,
  ordersId: string,
): Promise<void> {
  const cards = await api.all<{ name: string }>(
    `contracts/folders/${fraudId}/cards`,
  );
  if (cards.some((c) => c.name === "demo-analytics-team")) return;
  const created = await api.post(`contracts/folders/${fraudId}/cards`, {
    json: {
      name: "demo-analytics-team",
      description: "Fraud analytics, reads the Ledger orders.",
      contacts: [
        {
          name: "Analytics on call",
          kind: "email",
          value: "analytics@example.com",
        },
        {
          name: "Fraud lead",
          kind: "email",
          value: "fraud-lead@example.com",
        },
      ],
      email_enabled: false,
    },
  });
  const card = created.body as { id: string };
  await api.post(`contracts/cards/${card.id}/subscriptions`, {
    json: { contract_id: ordersId, version: "1.0.0" },
  });
}

const ENTRA_ALICE = "a11ce000-0000-4000-8000-000000000001";
// "Demo pipeline" in infra/fake-graph/directory.json; the API e2e suite registers "Pipeline" itself
// and needs its 201, so the demo never takes that one.
const ENTRA_PIPELINE = "de30b1ce-0000-4000-8000-000000000005";

async function ensureIdentities(
  api: Client,
  rootId: string,
  acmeId: string,
): Promise<void> {
  const users = await api.all<{ id: string; username: string }>(
    "identity/users",
  );
  if (!users.some((u) => u.username === "demo-alice"))
    await api.post("identity/users", {
      json: {
        username: "demo-alice",
        password: "Demo-Alice-2026-pass!",
        display_name: "Alice Demo",
      },
    });
  const groups = await api.all<{ id: string; name: string }>("identity/groups");
  let group = groups.find((g) => g.name === "acme-data-engineers");
  if (!group) {
    group = (
      await api.post("identity/groups", {
        json: { name: "acme-data-engineers", owner_folder_id: acmeId },
      })
    ).body as { id: string; name: string };
    await api.post(`identity/groups/${group.id}/members`, {
      json: { username: "demo-alice" },
      allowConflict: true,
    });
  }
  const accounts = await api.all<{ name: string }>("identity/service-accounts");
  if (!accounts.some((a) => a.name === "acme-pipeline"))
    await api.post("identity/service-accounts", {
      json: { owner_folder_id: acmeId, name: "acme-pipeline" },
    });
  // An Entra user registration has no owner folder; a service principal is owned by ROOT.
  await api.post("identity/entra/users", {
    json: { object_id: ENTRA_ALICE },
    allowConflict: true,
  });
  await api.post("identity/entra/service-principals", {
    json: { owner_folder_id: rootId, object_id: ENTRA_PIPELINE },
    allowConflict: true,
  });
}

/** Creates whatever part of the demo dataset is missing; `token` is an admin access token. */
export async function ensureDemoData(
  ctx: APIRequestContext,
  token: string,
): Promise<void> {
  const api = new Client(ctx, token, "demo seed");
  const folders = await ensureFolders(api);
  const acme = folders["acme"];
  const ledger = folders["acme/payments/ledger"];
  const fraud = folders["acme/payments/fraud"];
  if (!acme || !ledger || !fraud) throw new Error("demo seed: folders missing");
  const root = folders[""];
  if (!root) throw new Error("demo seed: no ROOT folder");
  await ensureIdentities(api, root.id, acme.id);
  const ordersId = await ensureOrders(api, ledger.id);
  await ensureTemplate(api, acme.id);
  await ensureCard(api, fraud.id, ordersId);
  // Last: once mandatory, the set warns on every later publish under Acme.
  await ensureRuleSet(api, acme.id);
}
