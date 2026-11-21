// Mounts the real route tree, router, API client and session at a path, signed in (the refresh
// cookie exchange answers 200), with msw standing in for the gateway. Screens' tests pass extra
// handlers; `/me`, capabilities, the folder list and an empty contract list have defaults that
// can be overridden.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render } from "@testing-library/react";
import type { RequestHandler } from "msw";
import { createApiClient } from "@/api/client";
import type { Folder, Me } from "@/api/types";
import { createSession } from "@/auth/session";
import type { RuntimeConfig } from "@/config";
import { createAppRouter } from "@/router";
import { http, HttpResponse, server } from "@/test/msw";

export const testConfig: RuntimeConfig = {
  entraEnabled: false,
  entraClientId: "",
  entraAuthority: "",
  entraScopes: [],
  entraProtocol: "oidc",
  localLoginEnabled: true,
  apiBase: "/api/v1",
};

export const ROOT_ID = "00000000-0000-4000-8000-000000000000";

export function makeMe(overrides: Partial<Me> = {}): Me {
  return {
    principal_id: "local:11111111-1111-4111-8111-111111111111",
    principal_type: "user",
    idp: "local",
    via: "jwt",
    display_name: "Ada Admin",
    username: "ada",
    preferred_username: null,
    read_only: false,
    interactive: true,
    groups: [],
    assignments: [
      {
        grant_id: "g-1",
        folder_id: ROOT_ID,
        folder_path: "",
        role: "global_admin",
        via_group_id: null,
      },
    ],
    global_permissions: [
      "folder:create",
      "folder:update",
      "folder:archive",
      "folder:delete",
      "user:read",
      "audit:list",
      "group:list",
    ],
    ...overrides,
  };
}

export function makeFolder(
  overrides: Partial<Folder> & Pick<Folder, "id">,
): Folder {
  return {
    parent_id: ROOT_ID,
    kind: "organization",
    slug: overrides.id,
    display_name: overrides.id,
    path: overrides.id,
    archived: false,
    archived_at: null,
    block_inheritance: false,
    created_by: "local:admin",
    created_at: "2026-11-01T10:00:00Z",
    updated_by: "local:admin",
    updated_at: "2026-11-01T10:00:00Z",
    ...overrides,
  };
}

export const rootFolder = makeFolder({
  id: ROOT_ID,
  parent_id: null,
  kind: "root",
  slug: "root",
  display_name: "ROOT",
  path: "",
});

export function renderApp(
  path: string,
  {
    me = makeMe(),
    folders = [rootFolder],
    permissions = [
      "folder:create",
      "folder:update",
      "folder:archive",
      "folder:delete",
      "folder:unarchive",
    ],
    handlers = [],
    config: configOverrides = {},
  }: {
    me?: Me;
    folders?: Folder[];
    permissions?: string[];
    handlers?: RequestHandler[];
    /** Runtime config keys to change from `testConfig` (for example `entraEnabled: true`). */
    config?: Partial<RuntimeConfig>;
  } = {},
) {
  server.use(
    ...handlers,
    http.post("*/api/v1/identity/auth/refresh", () =>
      HttpResponse.json({
        access_token: "access-1",
        expires_in: 900,
        scope: "",
        token_type: "Bearer",
      }),
    ),
    http.get("*/api/v1/identity/me", () => HttpResponse.json(me)),
    // Display names for principal ids: none by default, so screens show the short id.
    http.post("*/api/v1/identity/principals/names", () =>
      HttpResponse.json({ items: [] }),
    ),
    // The Sessions card on /account/profile: an empty list by default.
    http.get("*/api/v1/identity/me/sessions", () =>
      HttpResponse.json({
        items: [],
        total: 0,
        max_sessions: 5,
        access_token_lifetime_s: 900,
      }),
    ),
    // The Sessions card on /admin/users/$userId (a Global admin): an empty list by default.
    http.get("*/api/v1/identity/users/:id/sessions", () =>
      HttpResponse.json({
        items: [],
        total: 0,
        max_sessions: 5,
        access_token_lifetime_s: 900,
      }),
    ),
    http.get("*/api/v1/identity/me/capabilities", ({ request }) =>
      HttpResponse.json({
        folder_id: new URL(request.url).searchParams.get("folder_id"),
        roles: ["global_admin"],
        permissions,
        inheritance_blocked: false,
        grantable_roles: [],
      }),
    ),
    http.get("*/api/v1/identity/folders", ({ request }) => {
      const archived =
        new URL(request.url).searchParams.get("include_archived") === "true";
      return HttpResponse.json({
        items: folders.filter((folder) => archived || !folder.archived),
        next_cursor: null,
      });
    }),
    // The principal lists the picker and the name map page through: empty by default.
    ...[
      "users",
      "groups",
      "service-accounts",
      "entra/users",
      "entra/groups",
      "entra/service-principals",
    ].map((list) =>
      http.get(`*/api/v1/identity/${list}`, () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    ),
    // The shell's announcement banner and the Register pre-fill read the public settings: none by
    // default (a test that exercises them overrides this).
    http.get("*/api/v1/identity/settings/public", () =>
      HttpResponse.json({
        banner: null,
        entra_default_tenant_id: null,
        entra_default_owner_folder_id: null,
        default_odcs_api_version: "v3.2.0",
        require_lifecycle_reason: false,
        credential_max_ttl_days: 365,
        password_rules: {
          min_length: 12,
          require_upper: false,
          require_lower: false,
          require_digit: false,
          require_symbol: false,
        },
      }),
    ),
    // The sign-in, rotate and change-password screens read the public policy: both methods on and
    // today's password rules by default.
    http.get("*/api/v1/identity/auth/policy", () =>
      HttpResponse.json({
        methods: { local: true, microsoft: true },
        password_rules: {
          min_length: 12,
          require_upper: false,
          require_lower: false,
          require_digit: false,
          require_symbol: false,
        },
      }),
    ),
    // The Groups list counts each local group's members; none by default (a group page's own
    // handler wins when a test lists members).
    http.get("*/api/v1/identity/groups/:id/members", () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    ),
    // The /folders navigator counts contracts per visible row; an empty page by default.
    http.get("*/api/v1/contracts/contracts", () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    ),
    // The delete check a folder:delete holder's header asks: deletable by default (screens override).
    http.get("*/api/v1/identity/folders/:id/deletion-check", ({ params }) =>
      HttpResponse.json({
        folder_id: params.id,
        deletable: true,
        blockers: [],
        purges: [],
      }),
    ),
    http.get("*/api/v1/identity/folders/:id", ({ params }) => {
      const folder = folders.find((candidate) => candidate.id === params.id);
      return folder
        ? HttpResponse.json(folder)
        : HttpResponse.json(
            {
              type: "urn:dc:problem:not-found",
              title: "Not Found",
              status: 404,
            },
            {
              status: 404,
              headers: { "Content-Type": "application/problem+json" },
            },
          );
    }),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const config: RuntimeConfig = { ...testConfig, ...configOverrides };
  const api = createApiClient(config);
  const session = createSession(api, { navigate: () => undefined });
  const context = { config, queryClient, api, session };
  const router = createAppRouter(context);
  router.update({
    context,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, session, queryClient };
}
