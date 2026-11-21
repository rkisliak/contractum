// Shared msw fixtures for the Settings screen tests (features/settings): the settings list with
// an Entra and a Platform section, the Entra overview, About, a problem answer and the default
// handler stack.
import type { SettingsOut } from "@/api/types";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, makeMe, rootFolder } from "@/test/renderApp";

export const LOAD = { timeout: 10_000 };
export const TENANT = "11111111-2222-4333-8444-555555555555";
export const OTHER = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
export const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
export const team = makeFolder({
  id: "team-id",
  kind: "team",
  display_name: "Team",
  path: "acme.team",
});
export const folders = [rootFolder, acme, team];
export const admin = makeMe({
  global_permissions: [
    "settings:read",
    "settings:write",
    "audit:list",
    "group:list",
  ],
});

export const SETTINGS = "*/api/v1/identity/settings";

export function section(
  name: string,
  values: Record<string, unknown>,
  revision = 0,
): SettingsOut["sections"][number] {
  return {
    section: name,
    values,
    defaults:
      name === "entra" ? { default_tenant_id: TENANT } : { banner: null },
    defaults_source: name === "entra" ? "identity" : "built-in",
    revision,
    etag: `"${name}-r${String(revision)}"`,
    updated_at: null,
    updated_by: null,
  };
}

export function settingsBody(
  entra: Record<string, unknown> = {},
  platform: Record<string, unknown> = {},
): SettingsOut {
  return {
    sections: [section("entra", entra, 1), section("platform", platform, 2)],
    etag: '"all-0123456789abcdef"',
    choices: {
      entra_tenants: [TENANT, OTHER],
      odcs_api_versions: ["v3.1.0", "v3.2.0"],
      microsoft_signin_available: true,
    },
  };
}

export const overviewBody = {
  tenants: [
    {
      tenant_id: TENANT,
      issuer: "https://login.example/v2.0",
      audiences: ["api"],
    },
  ],
  graph_client_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  graph_configured: true,
  graph_secret_configured: true,
  signin_configured: true,
  secret_expiry: {
    status: "known",
    expires_at: "2099-01-01T00:00:00Z",
    days_left: 900,
    reason: null,
  },
};

export const aboutBody = {
  services: [
    {
      service: "identity",
      status: "ok",
      version: "0.1.0",
      commit: "abc1234",
      built_at: "dev",
      migration_head: "0011_platform_settings",
      database: "ok",
      setting_defaults: {},
    },
    {
      service: "contracts",
      status: "ok",
      version: "0.1.0",
      commit: "abc1234",
      built_at: "dev",
      migration_head: "0009",
      database: "ok",
      setting_defaults: {},
    },
    {
      service: "validation",
      status: "ok",
      version: "0.1.0",
      commit: "abc1234",
      built_at: "dev",
      migration_head: "0004",
      database: "ok",
      setting_defaults: {},
    },
    {
      service: "audit",
      status: "ok",
      version: "0.1.0",
      commit: "abc1234",
      built_at: "dev",
      migration_head: "0002",
      database: "ok",
      setting_defaults: {},
    },
    {
      service: "mail",
      status: "unreachable",
      version: null,
      commit: null,
      built_at: null,
      migration_head: null,
      database: null,
      setting_defaults: {},
    },
  ],
};

export function problem(
  status: number,
  slug: string,
  extra: Record<string, unknown> = {},
) {
  return HttpResponse.json(
    {
      type: `urn:dc:problem:${slug}`,
      title: "Refused",
      status,
      detail: "No.",
      ...extra,
    },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

export function stack(
  entra: Record<string, unknown> = {},
  platform: Record<string, unknown> = {},
) {
  return [
    http.get(SETTINGS, () => HttpResponse.json(settingsBody(entra, platform))),
    http.get(`${SETTINGS}/entra/overview`, () =>
      HttpResponse.json(overviewBody),
    ),
    http.get(`${SETTINGS}/about`, () => HttpResponse.json(aboutBody)),
  ];
}

/** GET /settings answering each body in turn (the last one repeats), counting the reads. */
export function settingsSequence(bodies: SettingsOut[]) {
  const reads = { count: 0 };
  const handler = http.get(SETTINGS, () => {
    const next = bodies[Math.min(reads.count, bodies.length - 1)];
    reads.count += 1;
    return HttpResponse.json(next);
  });
  return { handler, reads };
}

/** A 204 with no body: the client resolves without data, which every settings read refuses. */
export const noBody = () => new HttpResponse(null, { status: 204 });
