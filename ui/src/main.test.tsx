// The boot entry (D-UI-08-05): the loading screen first, then the app once /config.json is valid,
// MSAL only when Entra is on, and the retry screen when the config cannot be loaded. main.tsx runs
// on import, so every test mounts a fresh #root and imports a fresh copy of the module.
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionOptions } from "@/auth/session";
import type { RuntimeConfig } from "@/config";
import { RuntimeConfigError } from "@/config";
import type { RouterContext } from "@/routes/__root";
import { testConfig } from "@/test/renderApp";

const boot = vi.hoisted(() => ({
  loadRuntimeConfig: vi.fn<() => Promise<RuntimeConfig>>(),
  getMsal: vi.fn(() => Promise.resolve({})),
  hasEntraAccount: vi.fn(() => Promise.resolve(true)),
  acquireEntraToken: vi.fn(() =>
    Promise.resolve({ accessToken: "entra-1", expiresInS: 600 }),
  ),
  entraSignOut: vi.fn(() => Promise.resolve()),
  createSession: vi.fn(),
  routerContexts: [] as RouterContext[],
  navigate: vi.fn(),
}));

vi.mock("@/config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/config")>()),
  loadRuntimeConfig: boot.loadRuntimeConfig,
}));

vi.mock("@/auth/msal", () => ({
  getMsal: boot.getMsal,
  hasEntraAccount: boot.hasEntraAccount,
  acquireEntraToken: boot.acquireEntraToken,
  entraSignOut: boot.entraSignOut,
}));

vi.mock("@/auth/session", () => ({ createSession: boot.createSession }));

// A one-route stand-in for the real tree, so the test sees that the app (not the boot screen)
// is mounted and which context it was built with.
vi.mock("@/router", () => ({
  createAppRouter: (context: RouterContext) => {
    boot.routerContexts.push(context);
    const router = createRouter({
      routeTree: createRootRoute({
        component: () => createElement("p", null, "App mounted"),
      }),
      context,
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });
    router.navigate = boot.navigate;
    return router;
  },
}));

function mountRoot(): HTMLElement {
  const container = document.createElement("div");
  container.id = "root";
  document.body.append(container);
  return container;
}

function sessionOptions(): SessionOptions {
  const call = boot.createSession.mock.calls.at(-1) as
    [unknown, SessionOptions] | undefined;
  if (call === undefined) throw new Error("createSession was not called");
  return call[1];
}

async function importMain(): Promise<void> {
  await import("@/main");
}

beforeEach(() => {
  vi.resetModules();
  boot.routerContexts.length = 0;
  boot.createSession.mockReturnValue({ dispose: vi.fn() });
});

afterEach(() => {
  document.body.replaceChildren();
  vi.clearAllMocks();
});

describe("main.tsx boot", () => {
  it("throws when index.html has no #root element", async () => {
    await expect(importMain()).rejects.toThrow(
      "index.html must provide a #root element",
    );
    expect(boot.loadRuntimeConfig).not.toHaveBeenCalled();
  });

  it("shows the loading screen until the config arrives, then mounts the app", async () => {
    let resolveConfig: (config: RuntimeConfig) => void = () => undefined;
    boot.loadRuntimeConfig.mockReturnValue(
      new Promise((resolve) => {
        resolveConfig = resolve;
      }),
    );
    mountRoot();
    await importMain();

    expect(await screen.findByRole("status")).toHaveTextContent("Loading");
    expect(screen.queryByText("App mounted")).not.toBeInTheDocument();

    resolveConfig(testConfig);
    expect(await screen.findByText("App mounted")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    // Entra is off: MSAL is never initialized and the session gets no Entra adapter.
    expect(boot.getMsal).not.toHaveBeenCalled();
    expect(sessionOptions().entra).toBeNull();
    const [context] = boot.routerContexts;
    expect(context?.config).toBe(testConfig);
    expect(context?.session).toBe(boot.createSession.mock.results[0]?.value);
  });

  it("routes the session's sign-in navigation through the app router", async () => {
    boot.loadRuntimeConfig.mockResolvedValue(testConfig);
    mountRoot();
    await importMain();
    await screen.findByText("App mounted");

    sessionOptions().navigate("/signin", { redirect: "/folders" });
    expect(boot.navigate).toHaveBeenCalledWith({
      to: "/signin",
      search: { redirect: "/folders" },
    });
  });

  it("initializes MSAL before the router and hands the session an Entra adapter", async () => {
    const entraConfig: RuntimeConfig = { ...testConfig, entraEnabled: true };
    boot.loadRuntimeConfig.mockResolvedValue(entraConfig);
    mountRoot();
    await importMain();
    await screen.findByText("App mounted");

    expect(boot.getMsal).toHaveBeenCalledWith(entraConfig);
    const entra = sessionOptions().entra;
    if (!entra) throw new Error("the session got no Entra adapter");
    await expect(entra.hasAccount()).resolves.toBe(true);
    await expect(entra.acquire()).resolves.toEqual({
      accessToken: "entra-1",
      expiresInS: 600,
    });
    await entra.signOut();
    expect(boot.hasEntraAccount).toHaveBeenCalledWith(entraConfig);
    expect(boot.acquireEntraToken).toHaveBeenCalledWith(entraConfig);
    expect(boot.entraSignOut).toHaveBeenCalledWith(entraConfig);
  });

  it("shows the retry screen when the config cannot be loaded, and Try again reloads the page", async () => {
    boot.loadRuntimeConfig.mockRejectedValue(
      new RuntimeConfigError("config.json answered HTTP 502"),
    );
    const reload = vi.fn();
    vi.stubGlobal("location", { reload });
    try {
      mountRoot();
      await importMain();

      expect(
        await screen.findByRole("heading", { name: "Can't reach the server" }),
      ).toBeInTheDocument();
      expect(boot.createSession).not.toHaveBeenCalled();
      screen.getByRole("button", { name: "Try loading again" }).click();
      expect(reload).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("does not turn an unexpected boot error into the retry screen", async () => {
    const unexpected = new TypeError("router exploded");
    boot.loadRuntimeConfig.mockRejectedValue(unexpected);
    // start() rethrows it, so it surfaces as the page's unhandled rejection (as in a browser).
    // Vitest's own listeners would fail the run on it, so they step aside for this one test.
    const vitestListeners = process.listeners("unhandledRejection");
    process.removeAllListeners("unhandledRejection");
    const rejected = new Promise<unknown>((resolve) => {
      process.once("unhandledRejection", resolve);
    });
    try {
      mountRoot();
      await importMain();
      await expect(rejected).resolves.toBe(unexpected);
    } finally {
      process.removeAllListeners("unhandledRejection");
      for (const listener of vitestListeners) {
        process.on("unhandledRejection", listener);
      }
    }
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Loading");
    });
    expect(
      screen.queryByRole("button", { name: "Try loading again" }),
    ).not.toBeInTheDocument();
  });
});
