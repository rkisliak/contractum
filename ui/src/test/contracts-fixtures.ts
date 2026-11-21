// Shared fixtures for the contract screen tests (src/features/contracts): one contract "Orders"
// (c-1) in the Ledger team under Acme > Payments, published versions, drafts, a YAML document, a
// problem response and the default handlers the contract layout reads.
import type { RequestHandler } from "msw";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, rootFolder } from "@/test/renderApp";

export const CONTRACT_API = "*/api/v1/contracts/contracts/c-1";

export const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
export const payments = makeFolder({
  id: "payments-id",
  parent_id: "acme-id",
  kind: "project",
  slug: "payments",
  display_name: "Payments",
  path: "acme/payments",
});
export const ledger = makeFolder({
  id: "ledger-id",
  parent_id: "payments-id",
  kind: "team",
  slug: "ledger",
  display_name: "Ledger",
  path: "acme/payments/ledger",
});
export const contractFolders = [rootFolder, acme, payments, ledger];

/** Every contract right at the folder. */
export const CONTRACT_ALL = [
  "contract:read",
  "contract:read-metadata",
  "contract:update",
  "contract:publish",
  "contract:delete",
  "contract:restore",
  "contract:create",
];
export const CONTRACT_READER = ["contract:read", "contract:read-metadata"];

export function makeContract(overrides: Record<string, unknown> = {}) {
  return {
    id: "c-1",
    folder_id: "ledger-id",
    organization_id: "acme-id",
    odcs_id: "urn:acme:ledger:orders",
    notation: "odcs",
    name: "Orders",
    status: "active",
    api_version: "v3.1.0",
    domain: "payments",
    tags: ["demo"],
    media_type: "application/yaml",
    content_sha256: null,
    size_bytes: null,
    content_redacted: false,
    template: null,
    origin: null,
    version: { number: 2, state: "draft" },
    latest_version: { version_id: "v-1.0.0", semver: "1.0.0" },
    created_by: "local:admin",
    created_at: "2026-11-01T10:00:00Z",
    updated_by: "local:admin",
    updated_at: "2026-11-02T10:00:00Z",
    ...overrides,
  };
}

export function makeVersion(
  semver: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    version_id: `v-${semver}`,
    contract_id: "c-1",
    semver,
    status: "older",
    lifecycle: "published",
    deprecated: false,
    deprecated_at: null,
    sunset_date: null,
    content_sha256: `sha-${semver}`,
    etag: `"e-${semver}"`,
    size_bytes: 320,
    media_type: "application/yaml",
    odcs_api_version: "v3.1.0",
    published_at: "2026-11-02T10:00:00Z",
    published_by: "local:admin",
    fetch_url: `/api/v1/contracts/contracts/c-1/versions/${semver}`,
    validation_run_id: null,
    pinned_rule_sets: null,
    engine_version: null,
    schema_sha: null,
    ...overrides,
  };
}

export function makeDraft(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    contract_id: "c-1",
    revision: 1,
    etag: `"${id}-r1"`,
    declared_version: "1.1.0",
    base_version_id: "v-1.0.0",
    odcs_api_version: "v3.1.0",
    media_type: "application/yaml",
    content_sha256: "x",
    size_bytes: 10,
    name: "Orders",
    created_by: "local:admin",
    created_at: "2026-11-02T10:00:00Z",
    updated_by: "local:bob",
    updated_at: "2026-11-02T10:00:00Z",
    ...overrides,
  };
}

export const CONTRACT_DOC =
  "apiVersion: v3.1.0\nkind: DataContract\nid: urn:acme:ledger:orders\nversion: 1.0.0\n";

/** An RFC 9457 problem response with the platform's `urn:dc:problem:<slug>` type. */
export function problemResponse(
  status: number,
  slug: string,
  extra: Record<string, unknown> = {},
) {
  return HttpResponse.json(
    {
      type: `urn:dc:problem:${slug}`,
      title: `Title of ${slug}`,
      status,
      detail: `${slug} detail`,
      ...extra,
    },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

/** A text document response (YAML unless another media type is given). */
export function textDocument(
  text: string,
  mediaType = "application/yaml",
  etag?: string,
) {
  return new HttpResponse(text, {
    headers: {
      "Content-Type": mediaType,
      ...(etag ? { ETag: etag } : {}),
    },
  });
}

/** The contract, its versions and its drafts: what the layout and most tabs read first. */
export function contractHandlers({
  contract = makeContract(),
  versions = [makeVersion("1.0.0", { status: "latest" })],
  drafts = [] as unknown[],
}: {
  contract?: Record<string, unknown>;
  versions?: unknown[];
  drafts?: unknown[];
} = {}): RequestHandler[] {
  return [
    http.get(CONTRACT_API, () => HttpResponse.json(contract)),
    http.get(`${CONTRACT_API}/versions`, () =>
      HttpResponse.json({ items: versions }),
    ),
    http.get(`${CONTRACT_API}/drafts`, () =>
      HttpResponse.json({ items: drafts }),
    ),
  ];
}
