import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("uiBuild", () => {
  it('reads "dev" when the build injected nothing (Vitest, pnpm dev)', async () => {
    const { uiBuild } = await import("@/buildInfo");
    expect(uiBuild).toEqual({ version: "dev", commit: "dev", builtAt: "dev" });
  });

  it("reads the identity vite.config.ts injected at build time", async () => {
    const injected = {
      version: "1.4.0",
      commit: "abc1234",
      builtAt: "2026-11-21T07:00:00Z",
    };
    vi.stubGlobal("__DC_BUILD__", injected);
    vi.resetModules();
    const { uiBuild } = await import("@/buildInfo");
    expect(uiBuild).toEqual(injected);
  });
});
