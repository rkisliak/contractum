// One shared msw node server for client and component tests (D-UI-08-30). setup.ts starts it with
// the "error" strategy for unhandled requests (msw 3 `onUnhandledFrame`, formerly
// `onUnhandledRequest`) and fails the running test on any unmocked call, resets the handlers after
// each test and closes it at the end; tests add handlers with `server.use(http.get(...))`.
//
// The two public, pre-sign-in reads every screen may make (the sign-in methods and password rules,
// and the public settings behind the banner and form defaults) are the server's base handlers, so
// a test that does not care about them still mocks them; `resetHandlers()` keeps them, and a test
// that exercises them overrides them with `server.use(...)`.
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type { components } from "@/api/schema.gen";

export { http, HttpResponse };

type Schemas = components["schemas"];

const passwordRules: Schemas["IdentityPasswordRulesOut"] = {
  min_length: 12,
  require_upper: false,
  require_lower: false,
  require_digit: false,
  require_symbol: false,
};

// Both sign-in methods on and today's password rules.
export const defaultAuthPolicy: Schemas["IdentityAuthPolicyOut"] = {
  methods: { local: true, microsoft: true },
  password_rules: passwordRules,
};

// No banner and the shipped defaults.
export const defaultPublicSettings: Schemas["IdentitySettingsPublicOut"] = {
  banner: null,
  entra_default_tenant_id: null,
  entra_default_owner_folder_id: null,
  default_odcs_api_version: "v3.2.0",
  require_lifecycle_reason: false,
  credential_max_ttl_days: 365,
  password_rules: passwordRules,
};

export const server = setupServer(
  http.get("*/api/v1/identity/auth/policy", () =>
    HttpResponse.json(defaultAuthPolicy),
  ),
  http.get("*/api/v1/identity/settings/public", () =>
    HttpResponse.json(defaultPublicSettings),
  ),
);
