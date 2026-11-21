import { afterEach, describe, expect, it, vi } from "vitest";
import { createApiClient } from "@/api/client";
import type { RuntimeConfig } from "@/config";
import { http, HttpResponse, server } from "@/test/msw";
import {
  createSession,
  type Session,
  type SessionChannel,
  type SessionOptions,
} from "./session";
import { tokenStore } from "./tokenStore";

const config: RuntimeConfig = {
  entraEnabled: false,
  entraClientId: "",
  entraAuthority: "",
  entraScopes: [],
  entraProtocol: "oidc",
  localLoginEnabled: true,
  apiBase: "/api/v1",
};

const problem = (slug: string, status: number) =>
  HttpResponse.json(
    { type: `urn:dc:problem:${slug}`, title: slug, status },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );

const sessions: Session[] = [];

function newSession(extra: Partial<SessionOptions> = {}) {
  const navigate = vi.fn();
  const api = createApiClient(config);
  const session = createSession(api, {
    navigate,
    channel: null,
    currentPath: () => "/folders/7?tab=access",
    ...extra,
  });
  sessions.push(session);
  return { navigate, session, api };
}

afterEach(() => {
  for (const session of sessions.splice(0)) session.dispose();
  tokenStore.clear();
  vi.useRealTimers();
});

describe("the API client bearer", () => {
  it("injects the held token, and nothing when none is held", async () => {
    const seen: (string | null)[] = [];
    server.use(
      http.get("*/api/v1/identity/me", ({ request }) => {
        seen.push(request.headers.get("Authorization"));
        return HttpResponse.json({});
      }),
    );
    const api = createApiClient(config);
    await api.GET("/api/v1/identity/me");
    tokenStore.set("held-token", 60);
    await api.GET("/api/v1/identity/me");
    expect(seen).toEqual([null, "Bearer held-token"]);
  });

  it("keeps an Authorization header the caller set", async () => {
    let seen: string | null = null;
    server.use(
      http.get("*/api/v1/identity/me", ({ request }) => {
        seen = request.headers.get("Authorization");
        return HttpResponse.json({});
      }),
    );
    tokenStore.set("held-token", 60);
    await createApiClient(config).GET("/api/v1/identity/me", {
      headers: { Authorization: "Bearer restricted" },
    });
    expect(seen).toBe("Bearer restricted");
  });
});

describe("session.signIn", () => {
  it("asks for cookie delivery and keeps only the access token in memory", async () => {
    let delivery: string | null = null;
    let body: unknown;
    server.use(
      http.post("*/api/v1/identity/auth/login", async ({ request }) => {
        delivery = request.headers.get("X-Token-Delivery");
        body = await request.json();
        return HttpResponse.json({
          access_token: "access-1",
          expires_in: 900,
          scope: "",
          token_type: "Bearer",
        });
      }),
    );
    const { session } = newSession();
    await expect(session.signIn("ada", "pw")).resolves.toEqual({
      kind: "signed-in",
    });
    expect(delivery).toBe("cookie");
    expect(body).toEqual({ username: "ada", password: "pw" });
    expect(tokenStore.get()?.token).toBe("access-1");
    expect(session.restrictedToken()).toBeNull();
  });

  it("keeps a password:rotate token apart from the session store", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/login", () =>
        HttpResponse.json({
          access_token: "restricted-1",
          expires_in: 300,
          scope: "password:rotate",
          token_type: "Bearer",
        }),
      ),
    );
    const { session } = newSession();
    await expect(session.signIn("admin", "secret")).resolves.toEqual({
      kind: "rotation-required",
    });
    expect(tokenStore.get()).toBeNull();
    expect(session.restrictedToken()).toBe("restricted-1");
    session.clearRestrictedToken();
    expect(session.restrictedToken()).toBeNull();
  });

  it("rejects with the typed Problem on a 401 and stores nothing", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/login", () =>
        problem("invalid-credentials", 401),
      ),
    );
    const { session } = newSession();
    await expect(session.signIn("ada", "bad")).rejects.toMatchObject({
      problem: { status: 401, slug: "invalid-credentials" },
    });
    expect(tokenStore.get()).toBeNull();
  });
});

describe("session.refresh and restore", () => {
  const refreshed = () =>
    HttpResponse.json({
      access_token: "access-2",
      expires_in: 900,
      scope: "",
      token_type: "Bearer",
    });

  it("posts the cookie opt-in header with no body and stores the new token", async () => {
    let delivery: string | null = null;
    let text = "unset";
    server.use(
      http.post("*/api/v1/identity/auth/refresh", async ({ request }) => {
        delivery = request.headers.get("X-Token-Delivery");
        text = await request.text();
        return refreshed();
      }),
    );
    const { session } = newSession();
    await expect(session.refresh()).resolves.toBe(true);
    expect(delivery).toBe("cookie");
    expect(text).toBe("");
    expect(tokenStore.get()?.token).toBe("access-2");
  });

  it("resolves false on a 401 and clears a stale token", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () =>
        problem("invalid-credentials", 401),
      ),
    );
    tokenStore.set("stale", 1);
    const { session } = newSession();
    await expect(session.refresh()).resolves.toBe(false);
    expect(tokenStore.get()).toBeNull();
  });

  it("lets a server failure surface instead of reporting a signed-out user", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () =>
        problem("unavailable", 503),
      ),
    );
    const { session } = newSession();
    await expect(session.refresh()).rejects.toMatchObject({
      problem: { status: 503 },
    });
  });

  it("shares one request between concurrent callers so a cookie is never replayed", async () => {
    let calls = 0;
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () => {
        calls += 1;
        return refreshed();
      }),
    );
    const { session } = newSession();
    await Promise.all([
      session.refresh(),
      session.refresh(),
      session.restore(),
    ]);
    expect(calls).toBe(1);
    await session.refresh();
    expect(calls).toBe(2);
  });

  it("restore is true without a request when a token is held", async () => {
    tokenStore.set("held", 60);
    const { session } = newSession();
    await expect(session.restore()).resolves.toBe(true);
  });

  it("restore falls back to the cookie refresh", async () => {
    server.use(http.post("*/api/v1/identity/auth/refresh", refreshed));
    const { session } = newSession();
    await expect(session.restore()).resolves.toBe(true);
  });
});

describe("session.redirectToSignIn", () => {
  it("navigates with only the parts it was given", () => {
    const { session, navigate } = newSession();
    session.redirectToSignIn("/folders/1", "session-expired");
    session.redirectToSignIn();
    expect(navigate).toHaveBeenNthCalledWith(1, "/signin", {
      redirect: "/folders/1",
      notice: "session-expired",
    });
    expect(navigate).toHaveBeenNthCalledWith(2, "/signin", {});
  });
});

const tokenBody = (accessToken: string, expiresIn = 900) =>
  HttpResponse.json({
    access_token: accessToken,
    expires_in: expiresIn,
    scope: "",
    token_type: "Bearer",
  });

describe("the locked refresh", () => {
  it("runs every refresh inside the dc-refresh lock", async () => {
    const names: string[] = [];
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () => tokenBody("r1")),
    );
    const { session } = newSession({
      withLock: (name, fn) => {
        names.push(name);
        return fn();
      },
    });
    await session.refresh();
    expect(names).toEqual(["dc-refresh"]);
  });

  it("issues exactly one POST for three concurrent refresh calls and all resolve true", async () => {
    let posts = 0;
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () => {
        posts += 1;
        return tokenBody("r1");
      }),
    );
    const { session } = newSession();
    await expect(
      Promise.all([session.refresh(), session.refresh(), session.refresh()]),
    ).resolves.toEqual([true, true, true]);
    expect(posts).toBe(1);
  });

  it("builds the request only after the lock is granted", async () => {
    const order: string[] = [];
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () => {
        order.push("post");
        return tokenBody("r1");
      }),
    );
    let release: () => void = () => undefined;
    const granted = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { session } = newSession({
      withLock: async (_name, fn) => {
        order.push("waiting");
        await granted;
        order.push("granted");
        return fn();
      },
    });
    const pending = session.refresh();
    await Promise.resolve();
    expect(order).toEqual(["waiting"]);
    release();
    await pending;
    expect(order).toEqual(["waiting", "granted", "post"]);
  });
});

describe("the 401 retry", () => {
  const me = () => HttpResponse.json({ display_name: "Ada" });

  it("refreshes once, replays the request with the new bearer and resolves", async () => {
    const seen: (string | null)[] = [];
    let refreshes = 0;
    server.use(
      http.get("*/api/v1/identity/me", ({ request }) => {
        seen.push(request.headers.get("Authorization"));
        return request.headers.get("Authorization") === "Bearer fresh"
          ? me()
          : problem("token-expired", 401);
      }),
      http.post("*/api/v1/identity/auth/refresh", () => {
        refreshes += 1;
        return tokenBody("fresh");
      }),
    );
    const { api, navigate } = newSession();
    tokenStore.set("stale", 900);
    const { data } = await api.GET("/api/v1/identity/me");
    expect(data).toMatchObject({ display_name: "Ada" });
    expect(seen).toEqual(["Bearer stale", "Bearer fresh"]);
    expect(refreshes).toBe(1);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("replays a request with a body intact", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () => tokenBody("fresh")),
    );
    // A body is single use: the replay must come from the copy taken before the first send.
    server.use(
      http.post("*/api/v1/identity/users", async ({ request }) => {
        bodies.push(await request.json());
        return request.headers.get("Authorization") === "Bearer fresh"
          ? HttpResponse.json({}, { status: 201 })
          : problem("token-expired", 401);
      }),
    );
    const { api } = newSession();
    tokenStore.set("stale", 900);
    await api.POST("/api/v1/identity/users", {
      body: { username: "bob", display_name: "Bob" } as never,
    });
    expect(bodies).toEqual([
      { username: "bob", display_name: "Bob" },
      { username: "bob", display_name: "Bob" },
    ]);
  });

  it("expires the session after a second 401, once, with the current path and notice", async () => {
    server.use(
      http.get("*/api/v1/identity/me", () => problem("token-expired", 401)),
      http.post("*/api/v1/identity/auth/refresh", () => tokenBody("fresh")),
    );
    const { api, navigate } = newSession();
    tokenStore.set("stale", 900);
    await expect(api.GET("/api/v1/identity/me")).rejects.toMatchObject({
      problem: { status: 401 },
    });
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith("/signin", {
      redirect: "/folders/7?tab=access",
      notice: "session-expired",
    });
    expect(tokenStore.get()).toBeNull();
  });

  it("keeps the notice of a deliberate end when a late call then fails with 401", async () => {
    server.use(
      http.get("*/api/v1/identity/me", () => problem("token-expired", 401)),
      http.post("*/api/v1/identity/auth/refresh", () =>
        problem("invalid-credentials", 401),
      ),
    );
    const { api, navigate, session } = newSession();
    tokenStore.set("revoked", 900);
    session.end("password-changed");
    await expect(api.GET("/api/v1/identity/me")).rejects.toMatchObject({
      problem: { status: 401 },
    });
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith("/signin", {
      notice: "password-changed",
    });
    expect(tokenStore.get()).toBeNull();
  });

  it("expires the session when the refresh finds no session", async () => {
    server.use(
      http.get("*/api/v1/identity/me", () => problem("token-expired", 401)),
      http.post("*/api/v1/identity/auth/refresh", () =>
        problem("invalid-credentials", 401),
      ),
    );
    const { api, navigate } = newSession();
    tokenStore.set("stale", 900);
    await expect(api.GET("/api/v1/identity/me")).rejects.toMatchObject({
      problem: { status: 401 },
    });
    expect(navigate).toHaveBeenCalledTimes(1);
  });

  it("never retries a 401 from an auth endpoint", async () => {
    let refreshes = 0;
    server.use(
      http.post("*/api/v1/identity/auth/login", () =>
        problem("invalid-credentials", 401),
      ),
      http.post("*/api/v1/identity/auth/refresh", () => {
        refreshes += 1;
        return tokenBody("x");
      }),
    );
    const { session, navigate } = newSession();
    await expect(session.signIn("ada", "bad")).rejects.toMatchObject({
      problem: { status: 401 },
    });
    expect(refreshes).toBe(0);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not retry a call that carried its own Authorization header", async () => {
    let refreshes = 0;
    server.use(
      http.get("*/api/v1/identity/me", () => problem("token-expired", 401)),
      http.post("*/api/v1/identity/auth/refresh", () => {
        refreshes += 1;
        return tokenBody("x");
      }),
    );
    const { api } = newSession();
    await expect(
      api.GET("/api/v1/identity/me", {
        headers: { Authorization: "Bearer restricted" },
      }),
    ).rejects.toMatchObject({ problem: { status: 401 } });
    expect(refreshes).toBe(0);
  });

  it("shares one refresh between concurrent 401s", async () => {
    let refreshes = 0;
    server.use(
      http.get("*/api/v1/identity/me", ({ request }) =>
        request.headers.get("Authorization") === "Bearer fresh"
          ? me()
          : problem("token-expired", 401),
      ),
      http.post("*/api/v1/identity/auth/refresh", () => {
        refreshes += 1;
        return tokenBody("fresh");
      }),
    );
    const { api } = newSession();
    tokenStore.set("stale", 900);
    const results = await Promise.all([
      api.GET("/api/v1/identity/me"),
      api.GET("/api/v1/identity/me"),
      api.GET("/api/v1/identity/me"),
    ]);
    expect(results.every((result) => result.data !== undefined)).toBe(true);
    expect(refreshes).toBe(1);
  });

  it("lets a failing refresh surface its own problem and keeps the session", async () => {
    server.use(
      http.get("*/api/v1/identity/me", () => problem("token-expired", 401)),
      http.post("*/api/v1/identity/auth/refresh", () =>
        problem("unavailable", 503),
      ),
    );
    const { api, navigate } = newSession();
    tokenStore.set("stale", 900);
    await expect(api.GET("/api/v1/identity/me")).rejects.toMatchObject({
      problem: { status: 503 },
    });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("omits the redirect when already on the sign-in page and the notice when never signed in", async () => {
    server.use(
      http.get("*/api/v1/identity/me", () => problem("token-expired", 401)),
      http.post("*/api/v1/identity/auth/refresh", () =>
        problem("invalid-credentials", 401),
      ),
    );
    const { api, navigate } = newSession({ currentPath: () => "/signin" });
    await expect(api.GET("/api/v1/identity/me")).rejects.toMatchObject({
      problem: { status: 401 },
    });
    expect(navigate).toHaveBeenCalledWith("/signin", {});
  });
});

describe("the proactive refresh", () => {
  it("refreshes at 80% of expires_in (720 s for 900 s)", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    let refreshes = 0;
    server.use(
      http.post("*/api/v1/identity/auth/login", () => tokenBody("t1")),
      http.post("*/api/v1/identity/auth/refresh", () => {
        refreshes += 1;
        return tokenBody("t2");
      }),
    );
    const { session } = newSession();
    await session.signIn("ada", "pw");
    await vi.advanceTimersByTimeAsync(719_000);
    expect(refreshes).toBe(0);
    await vi.advanceTimersByTimeAsync(1_000);
    await vi.waitFor(() => {
      expect(refreshes).toBe(1);
    });
    expect(tokenStore.get()?.token).toBe("t2");
  });

  it("is cancelled when the token is cleared", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    let refreshes = 0;
    server.use(
      http.post("*/api/v1/identity/auth/login", () => tokenBody("t1")),
      http.post("*/api/v1/identity/auth/refresh", () => {
        refreshes += 1;
        return tokenBody("t2");
      }),
    );
    const { session } = newSession();
    await session.signIn("ada", "pw");
    tokenStore.clear();
    await vi.advanceTimersByTimeAsync(900_000);
    expect(refreshes).toBe(0);
  });

  it("expires the session when the proactive refresh is refused", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    server.use(
      http.post("*/api/v1/identity/auth/login", () => tokenBody("t1")),
      http.post("*/api/v1/identity/auth/refresh", () =>
        problem("invalid-credentials", 401),
      ),
    );
    const { session, navigate } = newSession();
    await session.signIn("ada", "pw");
    await vi.advanceTimersByTimeAsync(720_000);
    await vi.waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/signin", {
        redirect: "/folders/7?tab=access",
        notice: "session-expired",
      });
    });
  });
});

describe("sign-out across tabs", () => {
  function fakeChannel() {
    const posted: unknown[] = [];
    let listener: (event: MessageEvent) => void = () => undefined;
    const channel: SessionChannel = {
      postMessage: (message) => {
        posted.push(message);
      },
      close: vi.fn(),
      addEventListener: (_type, next) => {
        listener = next;
      },
    };
    return {
      channel,
      posted,
      receive: (data: unknown) => {
        listener(new MessageEvent("message", { data }));
      },
    };
  }

  it("posts logout on the cookie path, clears the store and broadcasts", async () => {
    let delivery: string | null = null;
    server.use(
      http.post("*/api/v1/identity/auth/logout", ({ request }) => {
        delivery = request.headers.get("X-Token-Delivery");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { channel, posted } = fakeChannel();
    const { session, navigate } = newSession({ channel });
    tokenStore.set("held", 900);
    await session.signOut();
    expect(delivery).toBe("cookie");
    expect(tokenStore.get()).toBeNull();
    expect(posted).toEqual(["logout"]);
    expect(navigate).toHaveBeenCalledWith("/signin", {});
  });

  it("keeps the session when the logout call fails", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/logout", () =>
        problem("unavailable", 503),
      ),
    );
    const { channel, posted } = fakeChannel();
    const { session, navigate } = newSession({ channel });
    tokenStore.set("held", 900);
    await expect(session.signOut()).rejects.toMatchObject({
      problem: { status: 503 },
    });
    expect(tokenStore.get()?.token).toBe("held");
    expect(posted).toEqual([]);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("drops the token and goes to /signin when another tab signs out", () => {
    const { channel, receive } = fakeChannel();
    const { navigate } = newSession({ channel });
    tokenStore.set("held", 900);
    receive("something-else");
    expect(tokenStore.get()?.token).toBe("held");
    receive("logout");
    expect(tokenStore.get()).toBeNull();
    expect(navigate).toHaveBeenCalledWith("/signin", {
      notice: "signed-out-elsewhere",
    });
  });
});

describe("an empty answer", () => {
  const empty = () => new HttpResponse(null, { status: 204 });

  it("rejects a sign-in whose response has no body and stores nothing", async () => {
    server.use(http.post("*/api/v1/identity/auth/login", empty));
    const { session } = newSession();
    await expect(session.signIn("ada", "pw")).rejects.toThrow(
      "The login response had no body",
    );
    expect(tokenStore.get()).toBeNull();
  });

  it("treats a refresh without a body as no session", async () => {
    server.use(http.post("*/api/v1/identity/auth/refresh", empty));
    const { session } = newSession();
    await expect(session.refresh()).resolves.toBe(false);
    expect(tokenStore.get()).toBeNull();
  });
});

describe("session.rotate", () => {
  it("posts the new password with the restricted bearer, then signs in on the cookie path", async () => {
    const logins: unknown[] = [];
    let rotation: { bearer: string | null; body: unknown } | null = null;
    server.use(
      http.post("*/api/v1/identity/auth/login", async ({ request }) => {
        const body = (await request.json()) as { password: string };
        logins.push(body);
        return body.password === "old"
          ? HttpResponse.json({
              access_token: "restricted-1",
              expires_in: 300,
              scope: "password:rotate",
              token_type: "Bearer",
            })
          : tokenBody("access-9");
      }),
      http.post("*/api/v1/identity/auth/password", async ({ request }) => {
        rotation = {
          bearer: request.headers.get("Authorization"),
          body: await request.json(),
        };
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { session } = newSession();
    await session.signIn("admin", "old");
    await expect(session.rotate("new-password-1")).resolves.toEqual({
      kind: "signed-in",
    });
    expect(rotation).toEqual({
      bearer: "Bearer restricted-1",
      body: { new_password: "new-password-1" },
    });
    expect(logins).toEqual([
      { username: "admin", password: "old" },
      { username: "admin", password: "new-password-1" },
    ]);
    expect(session.restrictedToken()).toBeNull();
    expect(tokenStore.get()?.token).toBe("access-9");
    expect(session.idp()).toBe("local");
  });

  it("refuses to rotate without a restricted sign-in", async () => {
    const { session } = newSession();
    await expect(session.rotate("new-password-1")).rejects.toThrow(
      "No restricted session to rotate",
    );
  });
});

describe("session.idp", () => {
  it("is null while no token is held, even after an earlier sign-in", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/login", () => tokenBody("t1")),
    );
    const { session } = newSession();
    expect(session.idp()).toBeNull();
    await session.signIn("ada", "pw");
    expect(session.idp()).toBe("local");
    tokenStore.clear();
    expect(session.idp()).toBeNull();
  });

  it("refuses an Entra renewal when the deployment gave no Entra adapter", async () => {
    const { session } = newSession();
    session.adoptEntraToken("entra-1", 3600);
    // No cookie refresh is attempted for an Entra session (msw would fail an unmocked call).
    await expect(session.refresh()).resolves.toBe(false);
  });
});

describe("the proactive refresh on a server failure", () => {
  it("keeps the still valid token and the session when the refresh fails", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    let refreshes = 0;
    server.use(
      http.post("*/api/v1/identity/auth/login", () => tokenBody("t1")),
      http.post("*/api/v1/identity/auth/refresh", () => {
        refreshes += 1;
        return problem("unavailable", 503);
      }),
    );
    const { session, navigate } = newSession();
    await session.signIn("ada", "pw");
    await vi.advanceTimersByTimeAsync(720_000);
    await vi.waitFor(() => {
      expect(refreshes).toBe(1);
    });
    expect(tokenStore.get()?.token).toBe("t1");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("schedules nothing when another holder clears the token as it is stored", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    let refreshes = 0;
    server.use(
      http.post("*/api/v1/identity/auth/login", () => tokenBody("t1")),
      http.post("*/api/v1/identity/auth/refresh", () => {
        refreshes += 1;
        return tokenBody("t2");
      }),
    );
    const stopClearing = tokenStore.subscribe(() => {
      tokenStore.clear();
    });
    try {
      const { session } = newSession();
      await session.signIn("ada", "pw");
    } finally {
      stopClearing();
    }
    expect(tokenStore.get()).toBeNull();
    await vi.advanceTimersByTimeAsync(900_000);
    expect(refreshes).toBe(0);
  });
});

describe("the browser defaults", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  function sessionWithDefaults() {
    const navigate = vi.fn();
    const session = createSession(createApiClient(config), { navigate });
    sessions.push(session);
    return { session, navigate };
  }

  it("broadcasts sign-out on the dc-session BroadcastChannel", async () => {
    const opened: { name: string; posted: unknown[] }[] = [];
    vi.stubGlobal(
      "BroadcastChannel",
      class FakeChannel {
        readonly record: { name: string; posted: unknown[] };
        constructor(name: string) {
          this.record = { name, posted: [] };
          opened.push(this.record);
        }
        postMessage(message: unknown) {
          this.record.posted.push(message);
        }
        addEventListener() {
          return undefined;
        }
        close() {
          return undefined;
        }
      },
    );
    server.use(
      http.post(
        "*/api/v1/identity/auth/logout",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );
    const { session } = sessionWithDefaults();
    await session.signOut();
    expect(opened).toEqual([{ name: "dc-session", posted: ["logout"] }]);
  });

  it("works without BroadcastChannel and returns to the page the browser is on", () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    window.history.replaceState(null, "", "/contracts/c-1?tab=versions#top");
    const { session, navigate } = sessionWithDefaults();
    session.expire();
    expect(navigate).toHaveBeenCalledWith("/signin", {
      redirect: "/contracts/c-1?tab=versions#top",
    });
  });
});
