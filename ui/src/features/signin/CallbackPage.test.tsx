// /auth/callback with MSAL mocked (UI-SPEC 7.2): ?start=1 begins the redirect, a completed
// redirect adopts the token and lands on the target after GET /me, a refused principal ends on
// the failure card with the API's problem, and a slow callback offers a way back.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createApiClient } from "@/api/client";
import { createSession } from "@/auth/session";
import { tokenStore } from "@/auth/tokenStore";
import { createAppRouter } from "@/router";
import { makeMe, testConfig } from "@/test/renderApp";
import { resetCallbackForTests } from "./CallbackPage";
import { http, HttpResponse, server } from "@/test/msw";

const msal = vi.hoisted(() => ({
  startEntraSignIn: vi.fn(() => Promise.resolve()),
  completeEntraSignIn: vi.fn(),
}));
vi.mock("@/auth/msal", async (original) => ({
  ...(await original<typeof import("@/auth/msal")>()),
  startEntraSignIn: msal.startEntraSignIn,
  completeEntraSignIn: msal.completeEntraSignIn,
}));

const config = { ...testConfig, entraEnabled: true, entraClientId: "ui-alice" };

function mount(path: string, { entraEnabled = true, strict = false } = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const pageConfig = { ...config, entraEnabled };
  const api = createApiClient(pageConfig);
  const session = createSession(api, { navigate: () => undefined });
  const context = { config: pageConfig, queryClient, api, session };
  const router = createAppRouter(context);
  router.update({
    context,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  const app = (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
  render(strict ? <StrictMode>{app}</StrictMode> : app);
  return { router, session };
}

afterEach(() => {
  resetCallbackForTests();
  tokenStore.clear();
  vi.useRealTimers();
  msal.startEntraSignIn.mockClear();
  msal.completeEntraSignIn.mockReset();
});

describe("the Entra callback", () => {
  it("starts the redirect with the target", async () => {
    mount("/auth/callback?start=1&redirect=%2Fcontracts");
    expect(await screen.findByText("Signing you in…")).toBeInTheDocument();
    expect(msal.startEntraSignIn).toHaveBeenCalledWith(config, "/contracts");
  });

  it("adopts the token, checks /me and lands on the target", async () => {
    msal.completeEntraSignIn.mockResolvedValue({
      accessToken: "entra-token",
      expiresInS: 3600,
      redirect: "/",
    });
    server.use(
      http.get("*/api/v1/identity/me", () =>
        HttpResponse.json(
          makeMe({ display_name: "Alice", idp: "entra", username: null }),
        ),
      ),
      http.get("*/api/v1/identity/me/capabilities", () =>
        HttpResponse.json({
          folder_id: null,
          roles: [],
          permissions: [],
          inheritance_blocked: false,
          grantable_roles: [],
        }),
      ),
      http.get("*/api/v1/identity/folders", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    );
    const { session } = mount("/auth/callback");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Welcome, Alice" }),
    ).toBeInTheDocument();
    expect(session.idp()).toBe("entra");
  });

  it("shows the API's refusal on the failure card", async () => {
    msal.completeEntraSignIn.mockResolvedValue({
      accessToken: "entra-token",
      expiresInS: 3600,
      redirect: "/",
    });
    server.use(
      http.get("*/api/v1/identity/me", () =>
        HttpResponse.json(
          {
            type: "urn:dc:problem:principal-not-registered",
            title: "Principal not registered",
            status: 403,
            detail: "Ask an administrator to register you.",
          },
          {
            status: 403,
            headers: { "Content-Type": "application/problem+json" },
          },
        ),
      ),
    );
    mount("/auth/callback");
    expect(
      await screen.findByText("Principal not registered"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Try loading again" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Back to sign in" }),
    ).toBeInTheDocument();
  });

  it("falls back to the fixed failure copy when MSAL rejects", async () => {
    msal.completeEntraSignIn.mockRejectedValue(new Error("access_denied"));
    mount("/auth/callback");
    expect(
      await screen.findByText("Microsoft sign-in did not complete"),
    ).toBeInTheDocument();
  });

  it("offers a way back when the callback is slow", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    msal.completeEntraSignIn.mockReturnValue(new Promise(() => undefined));
    mount("/auth/callback");
    expect(await screen.findByText("Signing you in…")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_500);
    });
    expect(
      screen.getByText("This is taking longer than expected."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Back to sign in" }),
    ).toBeInTheDocument();
  });
});

describe("the Entra callback failures and retries", () => {
  const failed = () => screen.findByText("Microsoft sign-in did not complete");

  it("fails at once without a request when Entra is off, and goes back to sign in", async () => {
    const user = userEvent.setup();
    const { router } = mount("/auth/callback", { entraEnabled: false });
    expect(await failed()).toBeInTheDocument();
    expect(msal.completeEntraSignIn).not.toHaveBeenCalled();
    expect(msal.startEntraSignIn).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Back to sign in" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/signin");
    });
  });

  it("shows the failure card when the redirect cannot start, and again when the retry cannot", async () => {
    const user = userEvent.setup();
    msal.startEntraSignIn.mockRejectedValue(new Error("popup blocked"));
    mount("/auth/callback?start=1&redirect=%2Faudit");
    expect(await failed()).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try loading again" }));
    await waitFor(() => {
      expect(msal.startEntraSignIn).toHaveBeenCalledTimes(2);
    });
    expect(await failed()).toBeInTheDocument();
    expect(msal.startEntraSignIn).toHaveBeenLastCalledWith(config, "/audit");
    msal.startEntraSignIn.mockResolvedValue(undefined);
  });

  it("fails when there is no redirect response, and the retry starts a new sign-in", async () => {
    const user = userEvent.setup();
    msal.completeEntraSignIn.mockResolvedValue(null);
    mount("/auth/callback?redirect=%2Fcards");
    expect(await failed()).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try loading again" }));
    expect(await screen.findByText("Signing you in…")).toBeInTheDocument();
    expect(msal.startEntraSignIn).toHaveBeenCalledWith(config, "/cards");
  });

  it("starts the redirect once under StrictMode's double effects", async () => {
    mount("/auth/callback?start=1", { strict: true });
    expect(await screen.findByText("Signing you in…")).toBeInTheDocument();
    expect(msal.startEntraSignIn).toHaveBeenCalledTimes(1);
  });

  it("adopts the token even when /me answers without a body", async () => {
    msal.completeEntraSignIn.mockResolvedValue({
      accessToken: "entra-token",
      expiresInS: 3600,
      redirect: "/signin/rotate",
    });
    server.use(
      http.get(
        "*/api/v1/identity/me",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );
    const { session, router } = mount("/auth/callback");
    await waitFor(() => {
      expect(session.idp()).toBe("entra");
    });
    await waitFor(() => {
      expect(router.state.location.pathname).not.toBe("/auth/callback");
    });
  });
});
