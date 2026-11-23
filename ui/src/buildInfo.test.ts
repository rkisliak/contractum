import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
  vi.doUnmock("@tanstack/react-router");
});

describe("uiImageBuild", () => {
  it('reads "dev" when the build injected nothing (Vitest, pnpm dev)', async () => {
    const { uiImageBuild } = await import("@/buildInfo");
    expect(uiImageBuild).toEqual({ commit: "dev", builtAt: "dev" });
  });

  it("reads the commit and time vite.config.ts injected at build time", async () => {
    const injected = {
      commit: "abc1234def56",
      builtAt: "2026-11-21T07:00:00Z",
    };
    vi.stubGlobal("__DC_BUILD__", injected);
    vi.resetModules();
    const { uiImageBuild } = await import("@/buildInfo");
    expect(uiImageBuild).toEqual(injected);
  });

  it("holds no version: that comes from the runtime config", async () => {
    vi.stubGlobal("__DC_BUILD__", {
      commit: "abc1234def56",
      builtAt: "2026-11-21T07:00:00Z",
    });
    vi.resetModules();
    const { uiImageBuild } = await import("@/buildInfo");
    expect("version" in uiImageBuild).toBe(false);
  });
});

describe("useUiBuild", () => {
  async function shown(config: { version?: string }) {
    vi.doMock("@tanstack/react-router", () => ({
      useRouteContext: () => ({ config }),
    }));
    vi.stubGlobal("__DC_BUILD__", {
      commit: "abc1234def56",
      builtAt: "2026-11-21T07:00:00Z",
    });
    vi.resetModules();
    const { useUiBuild } = await import("@/buildInfo");
    function Probe() {
      const build = useUiBuild();
      return createElement(
        "p",
        null,
        `${build.version} ${build.commit} ${build.builtAt}`,
      );
    }
    render(createElement(Probe));
  }

  it("takes the version from the runtime config and the rest from the image", async () => {
    await shown({ version: "1.4.0" });
    expect(
      screen.getByText("1.4.0 abc1234def56 2026-11-21T07:00:00Z"),
    ).toBeVisible();
  });

  it('reads "dev" when the config carries no version', async () => {
    await shown({});
    expect(
      screen.getByText("dev abc1234def56 2026-11-21T07:00:00Z"),
    ).toBeVisible();
  });
});
