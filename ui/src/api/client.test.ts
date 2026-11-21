import { afterEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import type { RuntimeConfig } from "@/config";
import { http, HttpResponse, server } from "@/test/msw";
import { baseUrlFor, createApiClient, installAuthRetry } from "./client";
import { isApiError } from "./problem";

function configWith(apiBase: string): RuntimeConfig {
  return {
    entraEnabled: false,
    entraClientId: "",
    entraAuthority: "",
    entraScopes: [],
    entraProtocol: "oidc",
    localLoginEnabled: true,
    apiBase,
  };
}

const me = {
  principal_id: "11111111-1111-4111-8111-111111111111",
  principal_type: "user",
  idp: "local",
  via: "jwt",
  display_name: "Ada Admin",
  username: "ada",
  preferred_username: null,
  read_only: false,
  interactive: true,
  groups: [],
  assignments: [],
  global_permissions: [],
} as const;

describe("createApiClient", () => {
  it("resolves typed data for GET /api/v1/identity/me", async () => {
    server.use(http.get("*/api/v1/identity/me", () => HttpResponse.json(me)));
    const client = createApiClient(configWith("/api/v1"));
    const { data, error } = await client.GET("/api/v1/identity/me");
    expect(error).toBeUndefined();
    // principal_id is typed string, so no cast is needed to read it.
    expect(data?.principal_id).toBe(me.principal_id);
    expect(data?.interactive).toBe(true);
  });

  it("rejects a path that is not in the generated document at type-check time", () => {
    const client = createApiClient(configWith("/api/v1"));
    // Never invoked: the assertion is the compile-time error that `pnpm typecheck` enforces.
    const neverCalled = () =>
      // @ts-expect-error "/api/v1/identity/no-such-route" is not a generated path
      client.GET("/api/v1/identity/no-such-route");
    expect(typeof neverCalled).toBe("function");
  });

  it.each([
    ["/api/v1", "/api/v1/identity/me"],
    ["/dc/api/v1", "/dc/api/v1/identity/me"],
  ])("with apiBase %s requests %s", async (apiBase, expected) => {
    const seen: string[] = [];
    server.use(
      http.get("*/identity/me", ({ request }) => {
        seen.push(new URL(request.url).pathname);
        return HttpResponse.json(me);
      }),
    );
    const client = createApiClient(configWith(apiBase));
    await client.GET("/api/v1/identity/me");
    expect(seen).toEqual([expected]);
  });

  it("uses the fetch it is given", async () => {
    const urls: string[] = [];
    const client = createApiClient(configWith("/api/v1"), {
      fetch: (input) => {
        urls.push(input.url);
        return Promise.resolve(Response.json(me));
      },
    });
    const { data } = await client.GET("/api/v1/identity/me");
    expect(data?.display_name).toBe("Ada Admin");
    expect(urls).toHaveLength(1);
  });
});

describe("baseUrlFor", () => {
  it.each([
    ["/api/v1", ""],
    ["/api/v1/", ""],
    ["/dc/api/v1", "/dc"],
    ["/custom", "/custom"],
  ])("maps %s to %j", (apiBase, expected) => {
    expect(baseUrlFor(apiBase)).toBe(expected);
  });
});

describe("bearer injection", () => {
  afterEach(() => {
    tokenStore.clear();
  });

  function recordingClient(seen: (string | null)[]) {
    return createApiClient(configWith("/api/v1"), {
      fetch: (input) => {
        seen.push(input.headers.get("Authorization"));
        return Promise.resolve(Response.json(me));
      },
    });
  }

  it("sends nothing while no token is held, then the held token", async () => {
    const seen: (string | null)[] = [];
    const client = recordingClient(seen);
    await client.GET("/api/v1/identity/me");
    tokenStore.set("access-1", 900);
    await client.GET("/api/v1/identity/me");
    expect(seen).toEqual([null, "Bearer access-1"]);
  });

  it("keeps an Authorization the caller set itself", async () => {
    tokenStore.set("access-1", 900);
    const seen: (string | null)[] = [];
    await recordingClient(seen).GET("/api/v1/identity/me", {
      headers: { Authorization: "Bearer rotation-only" },
    });
    expect(seen).toEqual(["Bearer rotation-only"]);
  });
});

describe("installAuthRetry", () => {
  type Step = (request: Request) => Response;
  const unauthorized: Step = () => new Response(null, { status: 401 });
  const ok: Step = () => Response.json(me);

  afterEach(() => {
    tokenStore.clear();
  });

  // A client whose fetch answers with the scripted steps in order and records what was sent.
  function scripted(steps: Step[], refreshResult = true) {
    const sent: { path: string; auth: string | null }[] = [];
    const client = createApiClient(configWith("/api/v1"), {
      fetch: (input) => {
        sent.push({
          path: new URL(input.url).pathname,
          auth: input.headers.get("Authorization"),
        });
        const step = steps.shift();
        if (step === undefined) throw new Error("unexpected extra request");
        return Promise.resolve(step(input));
      },
    });
    const refresh = vi.fn(() => {
      if (refreshResult) tokenStore.set("access-2", 900);
      return Promise.resolve(refreshResult);
    });
    const expire = vi.fn();
    const remove = installAuthRetry(client, { refresh, expire });
    return { client, sent, refresh, expire, remove };
  }

  async function statusOf(call: Promise<unknown>): Promise<number | null> {
    try {
      await call;
      return null;
    } catch (error) {
      return isApiError(error) ? error.problem.status : -1;
    }
  }

  it("passes a successful response through untouched", async () => {
    tokenStore.set("access-1", 900);
    const { client, refresh, sent } = scripted([ok]);
    const { data } = await client.GET("/api/v1/identity/me");
    expect(data?.display_name).toBe("Ada Admin");
    expect(refresh).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
  });

  it("refreshes once after a 401 and replays the request with the new bearer", async () => {
    tokenStore.set("access-1", 900);
    const { client, sent, refresh, expire } = scripted([unauthorized, ok]);
    const { data } = await client.GET("/api/v1/identity/me");
    expect(data?.principal_id).toBe(me.principal_id);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(expire).not.toHaveBeenCalled();
    expect(sent.map((entry) => entry.auth)).toEqual([
      "Bearer access-1",
      "Bearer access-2",
    ]);
  });

  it("refreshes an anonymous request too, since a session may exist in the cookie", async () => {
    const { client, sent, refresh } = scripted([unauthorized, ok]);
    await client.GET("/api/v1/identity/me");
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(sent.map((entry) => entry.auth)).toEqual([null, "Bearer access-2"]);
  });

  it("expires the session when a second 401 follows the replay", async () => {
    tokenStore.set("access-1", 900);
    const { client, expire, sent } = scripted([unauthorized, unauthorized]);
    expect(await statusOf(client.GET("/api/v1/identity/me"))).toBe(401);
    expect(expire).toHaveBeenCalledTimes(1);
    expect(sent).toHaveLength(2);
  });

  it("expires without a replay when there is no session to refresh", async () => {
    tokenStore.set("access-1", 900);
    const { client, expire, sent } = scripted([unauthorized], false);
    expect(await statusOf(client.GET("/api/v1/identity/me"))).toBe(401);
    expect(expire).toHaveBeenCalledTimes(1);
    expect(sent).toHaveLength(1);
  });

  it("just replays when another call refreshed while this one was in flight", async () => {
    tokenStore.set("access-1", 900);
    const refreshedMeanwhile: Step = () => {
      tokenStore.set("access-other", 900);
      return new Response(null, { status: 401 });
    };
    const { client, sent, refresh } = scripted([refreshedMeanwhile, ok]);
    const { data } = await client.GET("/api/v1/identity/me");
    expect(data?.username).toBe("ada");
    expect(refresh).not.toHaveBeenCalled();
    expect(sent.at(-1)?.auth).toBe("Bearer access-other");
  });

  it("does nothing when the token was cleared meanwhile (the session is already expiring)", async () => {
    tokenStore.set("access-1", 900);
    const clearedMeanwhile: Step = () => {
      tokenStore.clear();
      return new Response(null, { status: 401 });
    };
    const { client, refresh, expire, sent } = scripted([clearedMeanwhile]);
    expect(await statusOf(client.GET("/api/v1/identity/me"))).toBe(401);
    expect(refresh).not.toHaveBeenCalled();
    expect(expire).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
  });

  it("never retries identity's own auth endpoints", async () => {
    const { client, refresh, sent } = scripted([unauthorized]);
    const status = await statusOf(
      client.POST("/api/v1/identity/auth/logout", {}),
    );
    expect(status).toBe(401);
    expect(refresh).not.toHaveBeenCalled();
    expect(sent.map((entry) => entry.path)).toEqual([
      "/api/v1/identity/auth/logout",
    ]);
  });

  it("never retries a request that carried its own Authorization", async () => {
    const { client, refresh, sent } = scripted([unauthorized]);
    const status = await statusOf(
      client.GET("/api/v1/identity/me", {
        headers: { Authorization: "Bearer rotation-only" },
      }),
    );
    expect(status).toBe(401);
    expect(refresh).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
  });

  it("stops retrying once removed", async () => {
    tokenStore.set("access-1", 900);
    const { client, refresh, remove } = scripted([unauthorized]);
    remove();
    expect(await statusOf(client.GET("/api/v1/identity/me"))).toBe(401);
    expect(refresh).not.toHaveBeenCalled();
  });
});
