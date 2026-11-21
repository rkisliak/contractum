// MSAL adapter unit tests: `@azure/msal-browser` is mocked at the module boundary, so these
// tests pin the configuration handed to MSAL and the calls and token arithmetic around it.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeConfig } from "@/config";
import {
  acquireEntraToken,
  BRIDGE_PATH,
  buildMsalConfig,
  CALLBACK_PATH,
  completeEntraSignIn,
  entraSignOut,
  getMsal,
  hasEntraAccount,
  resetMsalForTests,
  startEntraSignIn,
} from "./msal";

const client = {
  initialize: vi.fn(),
  loginRedirect: vi.fn(),
  handleRedirectPromise: vi.fn(),
  setActiveAccount: vi.fn(),
  acquireTokenSilent: vi.fn(),
  getAllAccounts: vi.fn(),
  getActiveAccount: vi.fn(),
  logoutRedirect: vi.fn(),
  clearCache: vi.fn(),
};
const constructed: unknown[] = [];

vi.mock("@azure/msal-browser", () => ({
  // Called with `new`; returning an object from a constructor function yields that object.
  PublicClientApplication: function PublicClientApplication(
    configuration: unknown,
  ) {
    constructed.push(configuration);
    return client;
  },
}));

const config: RuntimeConfig = {
  entraEnabled: true,
  entraClientId: "client-1",
  entraAuthority: "https://idp.example.test/tenant-1",
  entraScopes: ["api://dc/.default"],
  entraProtocol: "oidc",
  localLoginEnabled: false,
  apiBase: "/api/v1",
};

const account = { homeAccountId: "acc-1", username: "ada@example.test" };
const NOW = 1_000_000;
const now = () => NOW;

beforeEach(() => {
  resetMsalForTests();
  constructed.length = 0;
  for (const fn of Object.values(client)) fn.mockReset();
  client.initialize.mockResolvedValue(undefined);
  client.handleRedirectPromise.mockResolvedValue(null);
  client.getAllAccounts.mockReturnValue([]);
  client.getActiveAccount.mockReturnValue(null);
});

afterEach(() => {
  resetMsalForTests();
});

describe("buildMsalConfig", () => {
  it("trusts the authority host and uses OIDC protocol mode for oidc", () => {
    expect(buildMsalConfig(config, "https://ui.example.test")).toEqual({
      auth: {
        clientId: "client-1",
        authority: "https://idp.example.test/tenant-1",
        knownAuthorities: ["idp.example.test"],
        redirectUri: `https://ui.example.test${BRIDGE_PATH}`,
        postLogoutRedirectUri: "https://ui.example.test/signin",
      },
      cache: { cacheLocation: "sessionStorage" },
      system: { protocolMode: "OIDC" },
    });
  });

  it("lists no known authority and uses AAD mode for aad, defaulting to the page origin", () => {
    const built = buildMsalConfig({ ...config, entraProtocol: "aad" });
    expect(built.auth.knownAuthorities).toEqual([]);
    expect(built.system?.protocolMode).toBe("AAD");
    expect(built.auth.redirectUri).toBe(
      `${window.location.origin}/redirect.html`,
    );
  });
});

describe("getMsal", () => {
  it("initializes one client and memoises it until reset", async () => {
    const first = await getMsal(config);
    const second = await getMsal(config);
    expect(first).toBe(client);
    expect(second).toBe(first);
    expect(constructed).toHaveLength(1);
    expect(client.initialize).toHaveBeenCalledTimes(1);
    expect(constructed[0]).toEqual(buildMsalConfig(config));

    resetMsalForTests();
    await getMsal(config);
    expect(constructed).toHaveLength(2);
  });
});

describe("startEntraSignIn", () => {
  it("redirects with the scopes, the callback start page and a safe state", async () => {
    await startEntraSignIn(config, "/contracts?q=x");
    expect(client.loginRedirect).toHaveBeenCalledWith({
      scopes: ["api://dc/.default"],
      redirectStartPage: `${window.location.origin}${CALLBACK_PATH}`,
      state: "/contracts?q=x",
    });
  });

  it("settles a leftover sign-out interaction before redirecting", async () => {
    const order: string[] = [];
    client.handleRedirectPromise.mockImplementation(() => {
      order.push("settle");
      return Promise.resolve(null);
    });
    client.loginRedirect.mockImplementation(() => {
      order.push("login");
      return Promise.resolve();
    });
    await startEntraSignIn(config);
    expect(client.handleRedirectPromise).toHaveBeenCalledWith({
      navigateToLoginRequestUrl: false,
    });
    expect(order).toEqual(["settle", "login"]);
  });

  it("still redirects when settling a stale interaction fails", async () => {
    client.handleRedirectPromise.mockRejectedValue(new Error("no_state"));
    await startEntraSignIn(config);
    expect(client.loginRedirect).toHaveBeenCalledTimes(1);
  });

  it("replaces an off-site redirect with /", async () => {
    await startEntraSignIn(config, "//evil.example");
    expect(client.loginRedirect.mock.calls[0]?.[0]).toMatchObject({
      state: "/",
    });
  });
});

describe("completeEntraSignIn", () => {
  it("resolves null when no redirect response is pending", async () => {
    client.handleRedirectPromise.mockResolvedValue(null);
    await expect(completeEntraSignIn(config, now)).resolves.toBeNull();
    expect(client.handleRedirectPromise).toHaveBeenCalledWith({
      navigateToLoginRequestUrl: false,
    });
    expect(client.setActiveAccount).not.toHaveBeenCalled();
  });

  it("returns the access token, its lifetime and the safe redirect", async () => {
    client.handleRedirectPromise.mockResolvedValue({
      account,
      accessToken: "at-1",
      expiresOn: new Date(NOW + 600_000),
      state: "/folders",
    });
    await expect(completeEntraSignIn(config, now)).resolves.toEqual({
      accessToken: "at-1",
      expiresInS: 600,
      redirect: "/folders",
    });
    expect(client.setActiveAccount).toHaveBeenCalledWith(account);
    expect(client.acquireTokenSilent).not.toHaveBeenCalled();
  });

  it("renews silently when the IdP returned no access token", async () => {
    client.handleRedirectPromise.mockResolvedValue({
      account,
      accessToken: "",
      expiresOn: null,
      state: "https://evil.example/",
    });
    client.acquireTokenSilent.mockResolvedValue({
      accessToken: "at-silent",
      expiresOn: null,
    });
    // No expiresOn: a five-minute lifetime is assumed.
    await expect(completeEntraSignIn(config, now)).resolves.toEqual({
      accessToken: "at-silent",
      expiresInS: 300,
      redirect: "/",
    });
    expect(client.acquireTokenSilent).toHaveBeenCalledWith({
      scopes: ["api://dc/.default"],
      account,
    });
  });

  it("rejects with MSAL's error when the IdP refused", async () => {
    const refused = new Error("access_denied");
    client.handleRedirectPromise.mockRejectedValue(refused);
    await expect(completeEntraSignIn(config, now)).rejects.toBe(refused);
  });
});

describe("hasEntraAccount", () => {
  it("is true only when the cache holds an account", async () => {
    await expect(hasEntraAccount(config)).resolves.toBe(false);
    client.getAllAccounts.mockReturnValue([account]);
    await expect(hasEntraAccount(config)).resolves.toBe(true);
  });
});

describe("acquireEntraToken", () => {
  it("returns null without an account", async () => {
    await expect(acquireEntraToken(config, now)).resolves.toBeNull();
    expect(client.acquireTokenSilent).not.toHaveBeenCalled();
  });

  it("uses the active account and floors the lifetime at one second", async () => {
    client.getActiveAccount.mockReturnValue(account);
    client.acquireTokenSilent.mockResolvedValue({
      accessToken: "at-2",
      expiresOn: new Date(NOW - 10_000),
    });
    await expect(acquireEntraToken(config, now)).resolves.toEqual({
      accessToken: "at-2",
      expiresInS: 1,
    });
    expect(client.setActiveAccount).toHaveBeenCalledWith(account);
  });

  it("falls back to the first cached account", async () => {
    const other = { homeAccountId: "acc-2" };
    client.getAllAccounts.mockReturnValue([other]);
    client.acquireTokenSilent.mockResolvedValue({
      accessToken: "at-3",
      expiresOn: new Date(NOW + 61_500),
    });
    await expect(acquireEntraToken(config, now)).resolves.toEqual({
      accessToken: "at-3",
      expiresInS: 61,
    });
    expect(client.acquireTokenSilent).toHaveBeenCalledWith({
      scopes: ["api://dc/.default"],
      account: other,
    });
  });

  it.each([
    [{ name: "InteractionRequiredAuthError" }],
    [{ errorCode: "interaction_required" }],
    [{ errorCode: "login_required" }],
    [{ errorCode: "consent_required" }],
    [{ errorCode: "no_tokens_found" }],
    [{ errorCode: "refresh_token_expired" }],
  ])("returns null when MSAL needs the user again (%o)", async (error) => {
    client.getActiveAccount.mockReturnValue(account);
    client.acquireTokenSilent.mockRejectedValue(error);
    await expect(acquireEntraToken(config, now)).resolves.toBeNull();
  });

  it.each([
    ["a network error", new Error("network")],
    ["an unrelated error code", { errorCode: "server_error" }],
    ["a non-string error code", { errorCode: 42 }],
    ["a string", "boom"],
    ["null", null],
  ])("rethrows %s", async (_label, error) => {
    client.getActiveAccount.mockReturnValue(account);
    client.acquireTokenSilent.mockRejectedValue(error);
    await expect(acquireEntraToken(config, now)).rejects.toBe(error);
  });
});

describe("entraSignOut", () => {
  it("logs out the active account back to /signin", async () => {
    client.getActiveAccount.mockReturnValue(account);
    await entraSignOut(config);
    expect(client.logoutRedirect).toHaveBeenCalledWith({
      account,
      postLogoutRedirectUri: `${window.location.origin}/signin`,
    });
    expect(client.clearCache).not.toHaveBeenCalled();
  });

  it("logs out without an account when none is cached", async () => {
    await entraSignOut(config);
    expect(client.logoutRedirect).toHaveBeenCalledWith({
      postLogoutRedirectUri: `${window.location.origin}/signin`,
    });
  });

  it("clears the tab cache when the IdP has no end-session endpoint", async () => {
    client.getAllAccounts.mockReturnValue([account]);
    client.logoutRedirect.mockRejectedValue(new Error("no end session"));
    await entraSignOut(config);
    expect(client.logoutRedirect.mock.calls[0]?.[0]).toMatchObject({
      account,
    });
    expect(client.clearCache).toHaveBeenCalledTimes(1);
  });
});
