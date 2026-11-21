// The Entra side of the session (D-UI-08-16): an adopted Entra token renews through the MSAL
// adapter (never the refresh cookie), a reload restores it from MSAL's cache, interaction
// required ends the session, and sign-out goes through the IdP without a POST /auth/logout.
import { afterEach, describe, expect, it, vi } from "vitest";
import { createApiClient } from "@/api/client";
import type { RuntimeConfig } from "@/config";
import { http, HttpResponse, server } from "@/test/msw";
import { createSession, type EntraAdapter, type Session } from "./session";
import { tokenStore } from "./tokenStore";

const config: RuntimeConfig = {
  entraEnabled: true,
  entraClientId: "ui-alice",
  entraAuthority: "https://localhost:8443/entra",
  entraScopes: ["openid"],
  entraProtocol: "oidc",
  localLoginEnabled: true,
  apiBase: "/api/v1",
};

const sessions: Session[] = [];

function newSession(entra: Partial<EntraAdapter> = {}) {
  const adapter: EntraAdapter = {
    hasAccount: vi.fn(() => Promise.resolve(false)),
    acquire: vi.fn(() =>
      Promise.resolve({ accessToken: "entra-2", expiresInS: 3600 }),
    ),
    signOut: vi.fn(() => Promise.resolve()),
    ...entra,
  };
  const navigate = vi.fn();
  const channel = {
    postMessage: vi.fn(),
    close: vi.fn(),
    addEventListener: vi.fn(),
  };
  const session = createSession(createApiClient(config), {
    navigate,
    channel,
    entra: adapter,
    currentPath: () => "/contracts",
  });
  sessions.push(session);
  return { session, adapter, navigate, channel };
}

afterEach(() => {
  for (const session of sessions.splice(0)) session.dispose();
  tokenStore.clear();
});

describe("an Entra session", () => {
  it("adopts the token and renews through MSAL, never the cookie", async () => {
    const cookieRefresh = vi.fn();
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () => {
        cookieRefresh();
        return HttpResponse.json({}, { status: 500 });
      }),
    );
    const { session, adapter } = newSession();
    session.adoptEntraToken("entra-1", 3600);
    expect(session.idp()).toBe("entra");
    expect(tokenStore.get()?.token).toBe("entra-1");
    await expect(session.refresh()).resolves.toBe(true);
    expect(adapter.acquire).toHaveBeenCalledOnce();
    expect(tokenStore.get()?.token).toBe("entra-2");
    expect(cookieRefresh).not.toHaveBeenCalled();
  });

  it("ends when MSAL needs the user again", async () => {
    const { session } = newSession({ acquire: () => Promise.resolve(null) });
    session.adoptEntraToken("entra-1", 3600);
    await expect(session.refresh()).resolves.toBe(false);
    expect(tokenStore.get()).toBeNull();
  });

  it("restores from MSAL's account cache on reload", async () => {
    const { session } = newSession({
      hasAccount: () => Promise.resolve(true),
    });
    await expect(session.restore()).resolves.toBe(true);
    expect(session.idp()).toBe("entra");
    expect(tokenStore.get()?.token).toBe("entra-2");
  });

  it("falls back to the cookie when MSAL has no account", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () =>
        HttpResponse.json({
          access_token: "local-1",
          token_type: "Bearer",
          expires_in: 900,
          scope: "",
        }),
      ),
    );
    const { session, adapter } = newSession();
    await expect(session.restore()).resolves.toBe(true);
    expect(adapter.acquire).not.toHaveBeenCalled();
    expect(session.idp()).toBe("local");
  });

  it("signs out through the IdP without the logout route", async () => {
    const logout = vi.fn();
    server.use(
      http.post("*/api/v1/identity/auth/logout", () => {
        logout();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { session, adapter, channel } = newSession();
    session.adoptEntraToken("entra-1", 3600);
    await session.signOut();
    expect(adapter.signOut).toHaveBeenCalledOnce();
    expect(logout).not.toHaveBeenCalled();
    expect(channel.postMessage).toHaveBeenCalledWith("logout");
    expect(tokenStore.get()).toBeNull();
  });
});

describe("an Entra restore that cannot complete", () => {
  const cookieAnswer = () =>
    HttpResponse.json({
      access_token: "local-1",
      token_type: "Bearer",
      expires_in: 900,
      scope: "",
    });

  it("falls back to the cookie when MSAL's account check fails", async () => {
    server.use(http.post("*/api/v1/identity/auth/refresh", cookieAnswer));
    const { session, adapter } = newSession({
      hasAccount: () => Promise.reject(new Error("cache unreadable")),
    });
    await expect(session.restore()).resolves.toBe(true);
    expect(adapter.acquire).not.toHaveBeenCalled();
    expect(session.idp()).toBe("local");
  });

  it("falls back to the cookie when MSAL holds an account but cannot renew it", async () => {
    server.use(http.post("*/api/v1/identity/auth/refresh", cookieAnswer));
    const { session, adapter } = newSession({
      hasAccount: () => Promise.resolve(true),
      acquire: vi.fn(() => Promise.reject(new Error("interaction required"))),
    });
    await expect(session.restore()).resolves.toBe(true);
    expect(adapter.acquire).toHaveBeenCalledOnce();
    expect(session.idp()).toBe("local");
    expect(tokenStore.get()?.token).toBe("local-1");
  });
});
