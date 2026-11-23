import { describe, expect, it, vi } from "vitest";
import { loadRuntimeConfig, RuntimeConfigError } from "@/config";

const valid = {
  entraEnabled: true,
  entraClientId: "client-1",
  entraAuthority: "https://login.example/tenant",
  entraScopes: "openid profile api://x/.default",
  entraProtocol: "aad",
  localLoginEnabled: false,
  apiBase: "/api/v1",
};

function respond(body: unknown, status = 200) {
  return vi.fn(() =>
    Promise.resolve(
      new Response(typeof body === "string" ? body : JSON.stringify(body), {
        status,
      }),
    ),
  );
}

describe("loadRuntimeConfig", () => {
  it("returns the typed config with entraScopes split on spaces", async () => {
    const fetchImpl = respond(valid);
    await expect(loadRuntimeConfig(fetchImpl)).resolves.toEqual({
      ...valid,
      entraScopes: ["openid", "profile", "api://x/.default"],
      version: "dev",
    });
    expect(fetchImpl).toHaveBeenCalledWith("/config.json", {
      cache: "no-store",
    });
  });

  it("yields an empty scope list for an empty scope string", async () => {
    const config = await loadRuntimeConfig(
      respond({ ...valid, entraScopes: "" }),
    );
    expect(config.entraScopes).toEqual([]);
  });

  it("rejects with RuntimeConfigError on a 404", async () => {
    await expect(
      loadRuntimeConfig(respond("nope", 404)),
    ).rejects.toBeInstanceOf(RuntimeConfigError);
  });

  it("rejects with RuntimeConfigError on a body that is not JSON", async () => {
    await expect(
      loadRuntimeConfig(respond("<html></html>")),
    ).rejects.toBeInstanceOf(RuntimeConfigError);
  });

  it("rejects with RuntimeConfigError when the request itself fails", async () => {
    const failing = vi.fn(() => Promise.reject(new TypeError("network down")));
    await expect(loadRuntimeConfig(failing)).rejects.toBeInstanceOf(
      RuntimeConfigError,
    );
  });

  it("rejects when the body is not an object", async () => {
    await expect(loadRuntimeConfig(respond([valid]))).rejects.toBeInstanceOf(
      RuntimeConfigError,
    );
    await expect(loadRuntimeConfig(respond(null))).rejects.toBeInstanceOf(
      RuntimeConfigError,
    );
  });

  it.each(Object.keys(valid))("rejects when %s is missing", async (key) => {
    const body = Object.fromEntries(
      Object.entries(valid).filter(([name]) => name !== key),
    );
    await expect(loadRuntimeConfig(respond(body))).rejects.toBeInstanceOf(
      RuntimeConfigError,
    );
  });

  it.each([
    ["entraEnabled", "true"],
    ["localLoginEnabled", 1],
    ["entraClientId", 7],
    ["entraAuthority", null],
    ["entraScopes", ["openid"]],
    ["apiBase", 5],
  ])("rejects when %s has the wrong type", async (key, value) => {
    await expect(
      loadRuntimeConfig(respond({ ...valid, [key]: value })),
    ).rejects.toBeInstanceOf(RuntimeConfigError);
  });

  it.each(["1.4.0", "1.4.0-rc.1", "dev", "9.9.9-rc.12", "1.0.0+build.5"])(
    "returns the platform version %s the entrypoint wrote",
    async (version) => {
      const config = await loadRuntimeConfig(respond({ ...valid, version }));
      expect(config.version).toBe(version);
    },
  );

  it("reads dev when the config has no version key", async () => {
    const config = await loadRuntimeConfig(respond(valid));
    expect(config.version).toBe("dev");
  });

  it.each([
    7,
    null,
    true,
    ["1.0.0"],
    "",
    "1.0 0",
    '1"',
    "1;2",
    "$host",
    "-1.0.0",
    ".1",
    "1\n2",
    "1".repeat(129),
  ])("rejects the version %j", async (version) => {
    await expect(
      loadRuntimeConfig(respond({ ...valid, version })),
    ).rejects.toBeInstanceOf(RuntimeConfigError);
  });

  it("rejects an entraProtocol outside aad and oidc", async () => {
    await expect(
      loadRuntimeConfig(respond({ ...valid, entraProtocol: "saml" })),
    ).rejects.toBeInstanceOf(RuntimeConfigError);
  });

  it.each(["https://evil.example/api", "//evil.example/api", "api/v1"])(
    "rejects the apiBase %s because it is not a same-origin path",
    async (apiBase) => {
      await expect(
        loadRuntimeConfig(respond({ ...valid, apiBase })),
      ).rejects.toBeInstanceOf(RuntimeConfigError);
    },
  );

  it("uses the browser fetch by default, never from the HTTP cache", async () => {
    const browserFetch = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify(valid)));
    try {
      await expect(loadRuntimeConfig()).resolves.toMatchObject({
        entraClientId: "client-1",
        apiBase: "/api/v1",
      });
      expect(browserFetch).toHaveBeenCalledWith("/config.json", {
        cache: "no-store",
      });
    } finally {
      browserFetch.mockRestore();
    }
  });
});
