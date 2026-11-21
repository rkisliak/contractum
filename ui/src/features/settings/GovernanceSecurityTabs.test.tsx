// Settings > Governance, Sign-in & security and the audit retention card (quick 261120-0ye).
// The API is stubbed with msw; every save is checked for its body and its If-Match.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import type { SettingsOut } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeMe, renderApp, rootFolder } from "@/test/renderApp";
import { problem, settingsSequence } from "@/test/settings-fixtures";

afterEach(() => {
  tokenStore.clear();
  window.sessionStorage.clear();
});

const LOAD = { timeout: 10_000 };
const SETTINGS = "*/api/v1/identity/settings";
const folders = [rootFolder];
const admin = makeMe({
  global_permissions: ["settings:read", "settings:write", "audit:list"],
});

type Values = Record<string, unknown>;

const SECURITY_DEFAULTS = {
  local_signin_enabled: true,
  microsoft_signin_enabled: true,
  credential_max_ttl_days: 365,
  max_active_tokens: 10,
  max_sessions_per_user: 5,
  session_max_s: 604800,
  idle_timeout_s: 43200,
  lockout_account_threshold: 5,
  lockout_ip_threshold: 20,
  lockout_max_lock_s: 900,
  password_min_length: 12,
  password_require_upper: false,
  password_require_lower: false,
  password_require_digit: false,
  password_require_symbol: false,
};

function section(
  name: string,
  values: Values,
  defaults: Values,
  revision = 1,
): SettingsOut["sections"][number] {
  return {
    section: name,
    values,
    defaults,
    defaults_source: name === "security" ? "identity" : "service",
    revision,
    etag: `"${name}-r${String(revision)}"`,
    updated_at: null,
    updated_by: null,
  };
}

function body(
  stored: { governance?: Values; security?: Values; platform?: Values } = {},
  options: { microsoft?: boolean; governanceDefaults?: Values } = {},
): SettingsOut {
  return {
    sections: [
      section("entra", {}, {}),
      section(
        "governance",
        stored.governance ?? {},
        options.governanceDefaults ?? {
          mandatory_warn_days: 14,
          default_odcs_api_version: "v3.2.0",
          require_lifecycle_reason: false,
        },
      ),
      section("security", stored.security ?? {}, SECURITY_DEFAULTS),
      section("platform", stored.platform ?? {}, {
        banner: null,
        audit_retention_days: null,
      }),
    ],
    etag: '"all-0123456789abcdef"',
    choices: {
      entra_tenants: [],
      odcs_api_versions: ["v3.1.0", "v3.2.0"],
      microsoft_signin_available: options.microsoft ?? true,
    },
  };
}

type Seen = { etag: string | null; body: unknown; section: string };

function stack(
  list: SettingsOut,
  onPut: (seen: Seen) => Response | Promise<Response>,
) {
  return [
    http.get(SETTINGS, () => HttpResponse.json(list)),
    http.get(`${SETTINGS}/about`, () => HttpResponse.json({ services: [] })),
    http.put(`${SETTINGS}/:section`, async ({ request, params }) =>
      onPut({
        etag: request.headers.get("if-match"),
        body: await request.json(),
        section: String(params.section),
      }),
    ),
  ];
}

function echo(name: string, values: Values, defaults: Values) {
  return HttpResponse.json(section(name, values, defaults, 2));
}

describe("the tab registry", () => {
  it("lists the five tabs in the fixed order", async () => {
    renderApp("/admin/settings", {
      me: admin,
      folders,
      handlers: stack(body(), () => problem(500, "internal")),
    });
    await screen.findByRole("tab", { name: "Entra" }, LOAD);
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Entra",
      "Governance",
      "Sign-in & security",
      "Platform",
    ]);
  });
});

describe("the Governance tab", () => {
  it("shows the defaults and saves only what differs, with If-Match", async () => {
    const actor = userEvent.setup();
    const seen: Seen[] = [];
    renderApp("/admin/settings?tab=governance", {
      me: admin,
      folders,
      handlers: stack(body(), (s) => {
        seen.push(s);
        return echo("governance", {}, {});
      }),
    });
    const warn = await screen.findByLabelText("Grace period (days)", {}, LOAD);
    expect(warn).toHaveValue("14");
    expect(
      screen.getByText("0 enforces new mandatory versions at once."),
    ).toBeVisible();
    expect(screen.getByText("Default: 14 days")).toBeVisible();
    const version = screen.getByLabelText("Default ODCS version");
    expect(
      within(version)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["v3.1.0", "v3.2.0"]);

    // untouched values are not stored
    await actor.click(
      screen.getByRole("button", { name: "Save governance settings" }),
    );
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]).toEqual({
      etag: '"governance-r1"',
      body: {},
      section: "governance",
    });

    await actor.clear(warn);
    await actor.type(warn, "3");
    await actor.selectOptions(version, "v3.1.0");
    await actor.click(
      screen.getByLabelText("Require a reason to unpublish or deprecate"),
    );
    await actor.click(
      screen.getByRole("button", { name: "Save governance settings" }),
    );
    await waitFor(() => {
      expect(seen).toHaveLength(2);
    });
    expect(seen[1]?.body).toEqual({
      mandatory_warn_days: 3,
      default_odcs_api_version: "v3.1.0",
      require_lifecycle_reason: true,
    });
    expect(await screen.findByText("Governance settings saved.")).toBeVisible();
  });

  it("accepts zero and refuses a value outside 0 to 365 before sending", async () => {
    const actor = userEvent.setup();
    const seen: Seen[] = [];
    renderApp("/admin/settings?tab=governance", {
      me: admin,
      folders,
      handlers: stack(body(), (s) => {
        seen.push(s);
        return echo("governance", { mandatory_warn_days: 0 }, {});
      }),
    });
    const warn = await screen.findByLabelText("Grace period (days)", {}, LOAD);
    await actor.clear(warn);
    await actor.type(warn, "366");
    await actor.click(
      screen.getByRole("button", { name: "Save governance settings" }),
    );
    expect(
      await screen.findByText("Enter a whole number of days from 0 to 365."),
    ).toBeVisible();
    expect(seen).toEqual([]);
    await actor.clear(warn);
    await actor.type(warn, "0");
    await actor.click(
      screen.getByRole("button", { name: "Save governance settings" }),
    );
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({ mandatory_warn_days: 0 });
  });

  it("resets a stored field to its default and shows a 422 on the field", async () => {
    const actor = userEvent.setup();
    const seen: Seen[] = [];
    renderApp("/admin/settings?tab=governance", {
      me: admin,
      folders,
      handlers: stack(
        body({
          governance: {
            mandatory_warn_days: 3,
            require_lifecycle_reason: true,
          },
        }),
        (s) => {
          seen.push(s);
          return seen.length === 1
            ? problem(422, "validation-error", {
                errors: [
                  {
                    pointer: "/default_odcs_api_version",
                    detail:
                      "This ODCS version is not supported by the platform.",
                  },
                ],
              })
            : echo("governance", {}, {});
        },
      ),
    });
    await screen.findByLabelText("Grace period (days)", {}, LOAD);
    expect(screen.getByLabelText("Grace period (days)")).toHaveValue("3");
    await actor.click(
      screen.getByRole("button", {
        name: "Reset to default for the grace period",
      }),
    );
    expect(
      await screen.findByText(/not supported by the platform/),
    ).toBeVisible();
    // the reset kept the other stored field
    expect(seen[0]?.body).toEqual({ require_lifecycle_reason: true });
  });

  it("says the default is set by the service when none was reported", async () => {
    renderApp("/admin/settings?tab=governance", {
      me: admin,
      folders,
      handlers: stack(
        body(
          {},
          {
            governanceDefaults: {
              mandatory_warn_days: null,
              default_odcs_api_version: null,
              require_lifecycle_reason: null,
            },
          },
        ),
        () => problem(500, "internal"),
      ),
    });
    expect(
      await screen.findByText(
        "Default: set by the validation service",
        {},
        LOAD,
      ),
    ).toBeVisible();
    expect(screen.getByLabelText("Grace period (days)")).toHaveValue("");
  });
});

describe("the Governance tab, more states", () => {
  async function openGovernance(
    list: SettingsOut,
    onPut: (seen: Seen) => Response | Promise<Response>,
    extra: RequestHandler[] = [],
  ) {
    const actor = userEvent.setup();
    renderApp("/admin/settings?tab=governance", {
      me: admin,
      folders,
      handlers: [...extra, ...stack(list, onPut)],
    });
    await screen.findByLabelText("Grace period (days)", {}, LOAD);
    return actor;
  }
  const saveGovernance = () =>
    screen.getByRole("button", { name: "Save governance settings" });

  it("starts from a stored version, offers the built-in one without choices and resets version and reason", async () => {
    const seen: Seen[] = [];
    const list = body(
      {
        governance: {
          default_odcs_api_version: "v3.2.0",
          require_lifecycle_reason: false,
        },
      },
      {
        governanceDefaults: {
          mandatory_warn_days: 14,
          default_odcs_api_version: "v3.1.0",
          require_lifecycle_reason: true,
        },
      },
    );
    list.choices.odcs_api_versions = [];
    const actor = await openGovernance(list, (s) => {
      seen.push(s);
      return echo("governance", {}, {});
    });
    const version = screen.getByLabelText("Default ODCS version");
    expect(version).toHaveValue("v3.2.0");
    expect(
      within(version)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["v3.2.0"]);
    expect(screen.getByText("Default: required")).toBeVisible();
    await actor.click(
      screen.getByRole("button", {
        name: "Reset to default for the ODCS version",
      }),
    );
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({ require_lifecycle_reason: false });
    expect(
      await screen.findByText("Default ODCS version reset."),
    ).toBeVisible();
    await actor.click(
      screen.getByRole("button", {
        name: "Reset to default for the reason requirement",
      }),
    );
    await waitFor(() => {
      expect(seen).toHaveLength(2);
    });
    expect(seen[1]?.body).toEqual({ default_odcs_api_version: "v3.2.0" });
  });

  it("stores nothing for an emptied grace period and refuses letters", async () => {
    const seen: Seen[] = [];
    const actor = await openGovernance(body(), (s) => {
      seen.push(s);
      return echo("governance", {}, {});
    });
    const warn = screen.getByLabelText("Grace period (days)");
    await actor.clear(warn);
    await actor.type(warn, "two");
    await actor.click(saveGovernance());
    expect(
      await screen.findByText("Enter a whole number of days from 0 to 365."),
    ).toBeVisible();
    expect(seen).toEqual([]);
    await actor.clear(warn);
    await actor.click(saveGovernance());
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({});
  });

  it("puts a grace period 422 on its field and the rest in the alert", async () => {
    const actor = await openGovernance(body(), () =>
      problem(422, "validation-error", {
        errors: [
          { pointer: "/mandatory_warn_days", detail: "Too long a grace." },
          { pointer: "/mandatory_warn_days", detail: "Second complaint." },
          { pointer: "/elsewhere", detail: "Not a governance field." },
        ],
      }),
    );
    await actor.click(saveGovernance());
    expect(await screen.findByText("Not a governance field.")).toBeVisible();
    expect(
      screen.getByLabelText("Grace period (days)"),
    ).toHaveAccessibleDescription(expect.stringContaining("Too long a grace."));
  });

  it("shows a server failure in the alert without touching the fields", async () => {
    const actor = await openGovernance(body(), () => problem(500, "internal"));
    await actor.click(saveGovernance());
    expect(await screen.findByText("Refused")).toBeVisible();
    expect(screen.getByLabelText("Grace period (days)")).toHaveValue("14");
  });

  it("takes the stored values the next read returns after a save", async () => {
    // The second read is a new revision of the section, so the form takes its values.
    const second = body({ governance: { mandatory_warn_days: 30 } });
    second.sections = second.sections.map((entry) =>
      entry.section === "governance"
        ? { ...entry, revision: 2, etag: '"governance-r2"' }
        : entry,
    );
    const reads = settingsSequence([body(), second]);
    const actor = await openGovernance(
      body(),
      () => echo("governance", { mandatory_warn_days: 30 }, {}),
      [reads.handler],
    );
    await actor.click(saveGovernance());
    await waitFor(() => {
      expect(screen.getByLabelText("Grace period (days)")).toHaveValue("30");
    });
    expect(screen.getByText("Governance settings saved.")).toBeVisible();
  });
});

describe("the Sign-in & security tab", () => {
  async function openSecurity(
    list: SettingsOut,
    onPut: (seen: Seen) => Response | Promise<Response>,
  ) {
    const actor = userEvent.setup();
    renderApp("/admin/settings?tab=security", {
      me: admin,
      folders,
      handlers: stack(list, onPut),
    });
    await screen.findByLabelText("Local sign-in", {}, LOAD);
    return actor;
  }
  const save = () =>
    screen.getByRole("button", { name: "Save sign-in and security settings" });

  it("shows the active token limit with its default and stores a changed value", async () => {
    const seen: Seen[] = [];
    const actor = await openSecurity(body(), (s) => {
      seen.push(s);
      return echo("security", { max_active_tokens: 25 }, SECURITY_DEFAULTS);
    });
    const field = screen.getByLabelText(
      "Maximum active access tokens per owner",
    );
    expect(field).toHaveValue("10");
    expect(
      screen.getByText(
        "Each user, and each service account, may hold at most this many active tokens. Existing tokens above the limit are kept.",
      ),
    ).toBeVisible();
    await actor.clear(field);
    await actor.type(field, "25");
    await actor.click(save());
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({ max_active_tokens: 25 });
  });

  it("shows the session limit with its default, stores a changed value and resets it", async () => {
    const seen: Seen[] = [];
    const actor = await openSecurity(
      body({ security: { max_sessions_per_user: 8 } }),
      (s) => {
        seen.push(s);
        return seen.length === 1
          ? problem(422, "validation-error", {
              errors: [
                { pointer: "/max_sessions_per_user", detail: "Too many." },
              ],
            })
          : echo("security", {}, SECURITY_DEFAULTS);
      },
    );
    const field = screen.getByLabelText("Maximum sessions per user");
    expect(field).toHaveValue("8");
    expect(
      screen.getByText(
        "Signing in beyond this ends the user's oldest session. Existing sessions above a lowered limit end at that user's next sign-in.",
      ),
    ).toBeVisible();
    await actor.clear(field);
    await actor.type(field, "12");
    await actor.click(save());
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({ max_sessions_per_user: 12 });
    // a 422 pointer lands on the field
    expect(await screen.findByText("Too many.")).toBeVisible();
    await actor.click(
      screen.getByRole("button", { name: "Reset to default for sessions" }),
    );
    await waitFor(() => {
      expect(seen).toHaveLength(2);
    });
    expect(seen[1]?.body).toEqual({});
  });

  it("does not store the session limit when it equals the default", async () => {
    const seen: Seen[] = [];
    const actor = await openSecurity(body(), (s) => {
      seen.push(s);
      return echo("security", {}, SECURITY_DEFAULTS);
    });
    const field = screen.getByLabelText("Maximum sessions per user");
    expect(field).toHaveValue("5");
    await actor.clear(field);
    await actor.type(field, "5");
    await actor.click(save());
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({});
  });

  it("converts hours and minutes to seconds and stores only what differs", async () => {
    const seen: Seen[] = [];
    const actor = await openSecurity(body(), (s) => {
      seen.push(s);
      return echo("security", {}, SECURITY_DEFAULTS);
    });
    expect(screen.getByLabelText("Session length (hours)")).toHaveValue("168");
    expect(screen.getByLabelText("Idle timeout (minutes)")).toHaveValue("720");
    expect(screen.getByLabelText("Longest lock (minutes)")).toHaveValue("15");

    await actor.click(save());
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]).toEqual({
      etag: '"security-r1"',
      body: {},
      section: "security",
    });

    const session = screen.getByLabelText("Session length (hours)");
    await actor.clear(session);
    await actor.type(session, "12");
    const idle = screen.getByLabelText("Idle timeout (minutes)");
    await actor.clear(idle);
    await actor.type(idle, "30");
    const lock = screen.getByLabelText("Longest lock (minutes)");
    await actor.clear(lock);
    await actor.type(lock, "60");
    const ttl = screen.getByLabelText("Longest lifetime (days)");
    await actor.clear(ttl);
    await actor.type(ttl, "90");
    const attempts = screen.getByLabelText("Failed attempts per account");
    await actor.clear(attempts);
    await actor.type(attempts, "3");
    const length = screen.getByLabelText("Minimum length");
    await actor.clear(length);
    await actor.type(length, "14");
    await actor.click(screen.getByLabelText("Require a digit"));
    await actor.click(save());
    await waitFor(() => {
      expect(seen).toHaveLength(2);
    });
    expect(seen[1]?.body).toEqual({
      session_max_s: 43200,
      idle_timeout_s: 1800,
      lockout_max_lock_s: 3600,
      credential_max_ttl_days: 90,
      lockout_account_threshold: 3,
      password_min_length: 14,
      password_require_digit: true,
    });
    expect(
      await screen.findByText("Sign-in and security settings saved."),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Applies to new passwords only. Existing passwords keep working.",
      ),
    ).toBeVisible();
  });

  it("refuses out-of-range numbers and an idle timeout above the session length", async () => {
    const seen: Seen[] = [];
    const actor = await openSecurity(body(), (s) => {
      seen.push(s);
      return echo("security", {}, SECURITY_DEFAULTS);
    });
    const length = screen.getByLabelText("Minimum length");
    await actor.clear(length);
    await actor.type(length, "8");
    const session = screen.getByLabelText("Session length (hours)");
    await actor.clear(session);
    await actor.type(session, "2");
    const idle = screen.getByLabelText("Idle timeout (minutes)");
    await actor.clear(idle);
    await actor.type(idle, "300");
    await actor.click(save());
    expect(
      await screen.findByText("Enter a number from 12 to 128."),
    ).toBeVisible();
    expect(
      screen.getByText("The idle timeout must not exceed the session length."),
    ).toBeVisible();
    expect(seen).toEqual([]);
  });

  it("shows a lockout refusal as a warning with its detail and changes nothing", async () => {
    const actor = await openSecurity(body(), () =>
      problem(409, "signin-lockout-risk", {
        detail:
          "You are signed in with a local account; turn local sign-in off from a Microsoft session instead.",
        errors: [{ pointer: "/local_signin_enabled", detail: "x" }],
      }),
    );
    await actor.click(screen.getByLabelText("Local sign-in"));
    expect(screen.getByLabelText("Local sign-in")).not.toBeChecked();
    await actor.click(save());
    const warning = await screen.findByText("This change could lock you out");
    expect(warning).toBeVisible();
    expect(
      screen.getByText(/You are signed in with a local account/),
    ).toBeVisible();
    expect(screen.getByText(/Nothing was changed\./)).toBeVisible();
    // not a red problem alert
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("disables Microsoft sign-in with a reason when Entra is not configured", async () => {
    await openSecurity(body({}, { microsoft: false }), () =>
      problem(500, "internal"),
    );
    const microsoft = screen.getByLabelText("Microsoft sign-in");
    expect(microsoft).toBeDisabled();
    expect(microsoft).not.toBeChecked();
    expect(
      screen.getByText(
        "Microsoft sign-in is not configured for this deployment.",
      ),
    ).toBeVisible();
  });

  it("puts a server 422 on its field and resets the password rules", async () => {
    const seen: Seen[] = [];
    const actor = await openSecurity(
      body({
        security: { password_min_length: 14, password_require_symbol: true },
      }),
      (s) => {
        seen.push(s);
        return seen.length === 1
          ? problem(422, "validation-error", {
              errors: [
                { pointer: "/lockout_ip_threshold", detail: "Too low." },
              ],
            })
          : echo("security", {}, SECURITY_DEFAULTS);
      },
    );
    expect(screen.getByLabelText("Minimum length")).toHaveValue("14");
    expect(screen.getByLabelText("Require a symbol")).toBeChecked();
    await actor.click(save());
    expect(await screen.findByText("Too low.")).toBeVisible();
    await actor.click(
      screen.getByRole("button", {
        name: "Reset to default for password rules",
      }),
    );
    await waitFor(() => {
      expect(seen).toHaveLength(2);
    });
    expect(seen[1]?.body).toEqual({});
  });
  /** The list with the security section changed by `edit` (defaults, revision). */
  function withSecurity(
    edit: (entry: SettingsOut["sections"][number]) => void,
    stored: Values = {},
  ): SettingsOut {
    const list = body({ security: stored });
    const entry = list.sections.find((s) => s.section === "security");
    if (entry !== undefined) edit(entry);
    return list;
  }

  it("leaves a field empty and says the service decides when no default is reported", async () => {
    const seen: Seen[] = [];
    const actor = await openSecurity(
      withSecurity((entry) => {
        entry.defaults = {};
      }),
      (s) => {
        seen.push(s);
        return echo("security", {}, {});
      },
    );
    expect(screen.getByLabelText("Minimum length")).toHaveValue("");
    expect(screen.getAllByText(/^Default: set by the service/).length).toBe(9);
    // Without reported defaults a sign-in method is on and a password rule is off.
    expect(screen.getByLabelText("Local sign-in")).toBeChecked();
    await actor.click(save());
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({});
  });

  it("stores Microsoft sign-in switched off", async () => {
    const seen: Seen[] = [];
    const actor = await openSecurity(body(), (s) => {
      seen.push(s);
      return echo("security", {}, SECURITY_DEFAULTS);
    });
    await actor.click(screen.getByLabelText("Microsoft sign-in"));
    await actor.click(save());
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({ microsoft_signin_enabled: false });
  });

  it("puts a sign-in method 422 on its checkbox and the rest in the alert", async () => {
    const actor = await openSecurity(body(), () =>
      problem(422, "validation-error", {
        errors: [
          { pointer: "/local_signin_enabled", detail: "Keep one method on." },
          { pointer: "/local_signin_enabled", detail: "Repeated." },
          { pointer: "/unknown_key", detail: "Not a security field." },
        ],
      }),
    );
    await actor.click(save());
    expect(await screen.findByText("Not a security field.")).toBeVisible();
    expect(screen.getByLabelText("Local sign-in")).toHaveAccessibleDescription(
      expect.stringContaining("Keep one method on."),
    );
  });

  it("shows a server failure in the alert", async () => {
    const actor = await openSecurity(body(), () => problem(500, "internal"));
    await actor.click(save());
    expect(await screen.findByText("Refused")).toBeVisible();
    expect(screen.queryByText("This change could lock you out")).toBeNull();
  });

  it("words a lockout refusal that carries no detail", async () => {
    const actor = await openSecurity(body(), () =>
      problem(409, "signin-lockout-risk", { detail: undefined }),
    );
    await actor.click(save());
    expect(
      await screen.findByText(/This change could lock every admin out\./),
    ).toBeVisible();
  });

  it("takes the stored values of a new revision after a save", async () => {
    const reads = settingsSequence([
      body(),
      withSecurity(
        (entry) => {
          entry.revision = 2;
          entry.etag = '"security-r2"';
        },
        { password_min_length: 20 },
      ),
    ]);
    const actor = userEvent.setup();
    renderApp("/admin/settings?tab=security", {
      me: admin,
      folders,
      handlers: [
        reads.handler,
        ...stack(body(), () =>
          echo("security", { password_min_length: 20 }, SECURITY_DEFAULTS),
        ),
      ],
    });
    await screen.findByLabelText("Minimum length", {}, LOAD);
    await actor.click(save());
    await waitFor(() => {
      expect(screen.getByLabelText("Minimum length")).toHaveValue("20");
    });
    expect(
      screen.getByText("Sign-in and security settings saved."),
    ).toBeVisible();
  });
});

describe("audit retention on the Platform tab", () => {
  async function openPlatform(
    list: SettingsOut,
    onPut: (seen: Seen) => Response | Promise<Response>,
  ) {
    const actor = userEvent.setup();
    renderApp("/admin/settings?tab=platform", {
      me: admin,
      folders,
      handlers: stack(list, onPut),
    });
    await screen.findByRole("button", { name: "Save retention" }, LOAD);
    return actor;
  }

  it("keeps forever by default and stores N days, banner untouched", async () => {
    const seen: Seen[] = [];
    const actor = await openPlatform(
      body({ platform: { banner: { text: "Hello", severity: "info" } } }),
      (s) => {
        seen.push(s);
        return echo("platform", {}, {});
      },
    );
    expect(screen.getByText("Default: keep forever")).toBeVisible();
    await actor.click(
      screen.getByRole("button", { name: "Delete after N days" }),
    );
    const days = await screen.findByLabelText(
      "Delete events older than (days)",
    );
    expect(
      screen.getByText("At least 90 days. Settings changes are always kept."),
    ).toBeVisible();
    await actor.type(days, "30");
    await actor.click(screen.getByRole("button", { name: "Save retention" }));
    expect(
      await screen.findByText("Enter a whole number of days from 90 to 36500."),
    ).toBeVisible();
    expect(seen).toEqual([]);
    await actor.clear(days);
    await actor.type(days, "400");
    await actor.click(screen.getByRole("button", { name: "Save retention" }));
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({
      banner: { text: "Hello", severity: "info" },
      audit_retention_days: 400,
    });
    expect(seen[0]?.etag).toBe('"platform-r1"');
  });

  it("shows a stored retention, resets it and keeps the banner when it is saved", async () => {
    const seen: Seen[] = [];
    const actor = await openPlatform(
      body({
        platform: {
          banner: { text: "Hello", severity: "info" },
          audit_retention_days: 400,
        },
      }),
      (s) => {
        seen.push(s);
        return echo("platform", {}, {});
      },
    );
    expect(
      screen.getByLabelText("Delete events older than (days)"),
    ).toHaveValue("400");
    await actor.click(
      screen.getByRole("button", {
        name: "Reset to default for audit retention",
      }),
    );
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({
      banner: { text: "Hello", severity: "info" },
    });

    // saving the banner carries the retention along
    await actor.click(screen.getByRole("button", { name: "Save banner" }));
    await waitFor(() => {
      expect(seen).toHaveLength(2);
    });
    expect(seen[1]?.body).toEqual({
      banner: { text: "Hello", severity: "info" },
      audit_retention_days: 400,
    });
  });

  it("stores 0 to keep forever when the environment sets a retention", async () => {
    const seen: Seen[] = [];
    const list = body();
    const platform = list.sections.find((s) => s.section === "platform");
    if (platform !== undefined)
      platform.defaults = { audit_retention_days: 365 };
    const actor = await openPlatform(list, (s) => {
      seen.push(s);
      return echo("platform", {}, {});
    });
    // the card starts in "days" mode with the environment value
    expect(
      screen.getByLabelText("Delete events older than (days)"),
    ).toHaveValue("365");
    await actor.click(screen.getByRole("button", { name: "Keep forever" }));
    await actor.click(screen.getByRole("button", { name: "Save retention" }));
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({ audit_retention_days: 0 });
  });

  it("stores nothing for Keep forever when the environment sets no retention", async () => {
    const seen: Seen[] = [];
    const actor = await openPlatform(
      body({ platform: { audit_retention_days: 400 } }),
      (s) => {
        seen.push(s);
        return echo("platform", {}, {});
      },
    );
    await actor.click(screen.getByRole("button", { name: "Keep forever" }));
    await actor.click(screen.getByRole("button", { name: "Save retention" }));
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({});
  });

  it("refuses words and stores nothing for the environment's own number of days", async () => {
    const seen: Seen[] = [];
    const list = body();
    const platform = list.sections.find((s) => s.section === "platform");
    if (platform !== undefined)
      platform.defaults = { audit_retention_days: 365 };
    const actor = await openPlatform(list, (s) => {
      seen.push(s);
      return echo("platform", {}, {});
    });
    const days = screen.getByLabelText("Delete events older than (days)");
    await actor.clear(days);
    await actor.type(days, "ninety");
    await actor.click(screen.getByRole("button", { name: "Save retention" }));
    expect(
      await screen.findByText("Enter a whole number of days from 90 to 36500."),
    ).toBeVisible();
    await actor.clear(days);
    await actor.type(days, "365");
    await actor.click(screen.getByRole("button", { name: "Save retention" }));
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]?.body).toEqual({});
  });

  it("puts a retention 422 on the field and another refusal in the alert", async () => {
    let answer = () =>
      problem(422, "validation-error", {
        errors: [
          { pointer: "/audit_retention_days", detail: "Too short to keep." },
        ],
      });
    const actor = await openPlatform(
      body({ platform: { audit_retention_days: 400 } }),
      () => answer(),
    );
    await actor.click(screen.getByRole("button", { name: "Save retention" }));
    expect(await screen.findByText("Too short to keep.")).toBeVisible();
    answer = () => problem(500, "internal");
    await actor.click(screen.getByRole("button", { name: "Save retention" }));
    expect(await screen.findByText("Refused")).toBeVisible();
    expect(screen.queryByText("Too short to keep.")).toBeNull();
  });

  it("shows a refused banner save in the alert", async () => {
    const actor = await openPlatform(
      body({ platform: { banner: { text: "Hello", severity: "info" } } }),
      () => problem(412, "precondition-failed"),
    );
    await actor.click(screen.getByRole("button", { name: "Save banner" }));
    expect(
      await screen.findByText("These settings changed elsewhere"),
    ).toBeVisible();
  });
});
