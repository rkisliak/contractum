// The public sign-in policy in the browser (quick 261120-0ye): password rules listed and
// pre-checked on the change-password screen, the pure helpers, and the sign-in page offering only
// the methods the policy leaves on.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { PasswordRules } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { renderApp } from "@/test/renderApp";
import {
  DEFAULT_PASSWORD_RULES,
  describeRules,
  unmetRule,
} from "./passwordRules";

afterEach(() => {
  tokenStore.clear();
  window.sessionStorage.clear();
});

const SLOW = { timeout: 10_000 };
const POLICY = "*/api/v1/identity/auth/policy";

const strict: PasswordRules = {
  min_length: 14,
  require_upper: true,
  require_lower: true,
  require_digit: true,
  require_symbol: true,
};

function policy(
  rules: PasswordRules,
  methods: { local: boolean; microsoft: boolean } = {
    local: true,
    microsoft: true,
  },
) {
  return http.get(POLICY, () =>
    HttpResponse.json({ methods, password_rules: rules }),
  );
}

describe("password rule helpers", () => {
  it("describes the rules in the API's order", () => {
    expect(describeRules(DEFAULT_PASSWORD_RULES)).toEqual([
      "At least 12 characters",
    ]);
    expect(describeRules(strict)).toEqual([
      "At least 14 characters",
      "An upper-case letter",
      "A lower-case letter",
      "A digit",
      "A symbol",
    ]);
  });

  it("names the first unmet rule and accepts a conforming password", () => {
    expect(unmetRule(strict, "Short1!")).toBe("Use at least 14 characters.");
    expect(unmetRule(strict, "lowercase-only-1!!")).toBe(
      "Add an upper-case letter.",
    );
    expect(unmetRule(strict, "UPPERCASE-ONLY-1!!")).toBe(
      "Add a lower-case letter.",
    );
    expect(unmetRule(strict, "NoDigitsHere-abc!!")).toBe("Add a digit.");
    expect(unmetRule(strict, "NoSymbolsHere123abc")).toMatch(/^Add a symbol/);
    expect(unmetRule(strict, "Valid-Passw0rd-123")).toBeNull();
    // the default policy only counts length, in characters not bytes
    expect(unmetRule(DEFAULT_PASSWORD_RULES, "Ünïcödé-pässw")).toBeNull();
    expect(unmetRule(DEFAULT_PASSWORD_RULES, "eleven char")).toBe(
      "Use at least 12 characters.",
    );
  });
});

describe("the change-password screen", () => {
  it("lists the stored rules and refuses a password that breaks them before sending", async () => {
    const actor = userEvent.setup();
    let posted = false;
    renderApp("/account/password", {
      handlers: [
        policy({ ...DEFAULT_PASSWORD_RULES, min_length: 14 }),
        http.post("*/api/v1/identity/auth/password", () => {
          posted = true;
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    const rules = await screen.findByRole(
      "list",
      { name: "Password rules" },
      SLOW,
    );
    expect(
      await within(rules).findByText("At least 14 characters"),
    ).toBeVisible();
    await actor.type(screen.getByLabelText("Current password"), "old-secret");
    await actor.type(screen.getByLabelText("New password"), "twelve-chars");
    await actor.type(
      screen.getByLabelText("Confirm new password"),
      "twelve-chars",
    );
    await actor.click(screen.getByRole("button", { name: "Change password" }));
    expect(screen.getByText("Use at least 14 characters.")).toBeVisible();
    expect(posted).toBe(false);
  });

  it("falls back to the default rules when the policy cannot be read", async () => {
    renderApp("/account/password", {
      handlers: [
        http.get(POLICY, () => new HttpResponse(null, { status: 500 })),
      ],
    });
    const rules = await screen.findByRole(
      "list",
      { name: "Password rules" },
      SLOW,
    );
    expect(within(rules).getByText("At least 12 characters")).toBeVisible();
  });
});

describe("the sign-in page", () => {
  const config = { entraEnabled: true, localLoginEnabled: true } as const;

  it("offers both methods when the policy leaves both on", async () => {
    renderApp("/signin?notice=session-expired", {
      config,
      handlers: [policy(DEFAULT_PASSWORD_RULES)],
    });
    expect(await screen.findByLabelText("Username", {}, SLOW)).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Sign in with Microsoft" }),
    ).toBeVisible();
  });

  it("hides local sign-in when the policy turns it off", async () => {
    renderApp("/signin?notice=session-expired", {
      config,
      handlers: [
        policy(DEFAULT_PASSWORD_RULES, { local: false, microsoft: true }),
      ],
    });
    expect(
      await screen.findByRole(
        "button",
        { name: "Sign in with Microsoft" },
        SLOW,
      ),
    ).toBeVisible();
    expect(screen.queryByLabelText("Username")).toBeNull();
  });

  it("hides Microsoft sign-in when the policy turns it off", async () => {
    renderApp("/signin?notice=session-expired", {
      config,
      handlers: [
        policy(DEFAULT_PASSWORD_RULES, { local: true, microsoft: false }),
      ],
    });
    expect(await screen.findByLabelText("Username", {}, SLOW)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Sign in with Microsoft" }),
    ).toBeNull();
  });

  it("never offers a method the runtime config does not enable, whatever the policy says", async () => {
    renderApp("/signin?notice=session-expired", {
      config: { entraEnabled: false, localLoginEnabled: true },
      handlers: [policy(DEFAULT_PASSWORD_RULES)],
    });
    expect(await screen.findByLabelText("Username", {}, SLOW)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Sign in with Microsoft" }),
    ).toBeNull();
  });

  it("uses the configured methods when the policy call fails", async () => {
    renderApp("/signin?notice=session-expired", {
      config,
      handlers: [
        http.get(POLICY, () => new HttpResponse(null, { status: 503 })),
      ],
    });
    expect(await screen.findByLabelText("Username", {}, SLOW)).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Sign in with Microsoft" }),
    ).toBeVisible();
  });
});
