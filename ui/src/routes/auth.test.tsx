// The sign-in journey through the real route tree, router and API client with msw standing in
// for the gateway: restore-before-render, the redirect to /signin, and the sign-in page states.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApiClient } from "@/api/client";
import { createSession } from "@/auth/session";
import { tokenStore } from "@/auth/tokenStore";
import type { RuntimeConfig } from "@/config";
import { createAppRouter } from "@/router";
import { http, HttpResponse, server } from "@/test/msw";

const config: RuntimeConfig = {
  entraEnabled: false,
  entraClientId: "",
  entraAuthority: "",
  entraScopes: [],
  entraProtocol: "oidc",
  localLoginEnabled: true,
  apiBase: "/api/v1",
};

const tokens = {
  access_token: "access-1",
  expires_in: 900,
  scope: "",
  token_type: "Bearer",
};

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
};

const unauthorized = () =>
  HttpResponse.json(
    {
      type: "urn:dc:problem:invalid-credentials",
      title: "Invalid",
      status: 401,
    },
    { status: 401, headers: { "Content-Type": "application/problem+json" } },
  );

function mount(path: string, overrides: Partial<RuntimeConfig> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const pageConfig = { ...config, ...overrides };
  const api = createApiClient(pageConfig);
  const session = createSession(api, { navigate: () => undefined });
  const context = { config: pageConfig, queryClient, api, session };
  const router = createAppRouter(context);
  router.update({
    context,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router };
}

// The signed-in shell also loads the sidebar folder tree; these tests are about the session, so
// it answers with no folders.
beforeEach(() => {
  server.use(
    http.get("*/api/v1/identity/folders", () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    ),
  );
});

afterEach(() => {
  tokenStore.clear();
});

describe("the access token", () => {
  it("never touches Web Storage or cookies", () => {
    tokenStore.set("secret-token", 60);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });
});

describe("the authenticated layout", () => {
  it("redirects to /signin with the location when there is no session", async () => {
    server.use(http.post("*/api/v1/identity/auth/refresh", unauthorized));
    const { router } = mount("/");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Sign in" }),
    ).toBeVisible();
    expect(router.state.location.pathname).toBe("/signin");
    expect(router.state.location.search).toEqual({ redirect: "/" });
  });

  it("shows a skeleton while restoring and never flashes the sign-in page", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post("*/api/v1/identity/auth/refresh", async () => {
        await gate;
        return HttpResponse.json(tokens);
      }),
      http.get("*/api/v1/identity/me", () => HttpResponse.json(me)),
    );
    mount("/");
    expect(await screen.findByText("Loading session")).toBeInTheDocument();
    expect(screen.queryByText("Sign in")).not.toBeInTheDocument();
    release();
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Welcome, Ada Admin",
      }),
    ).toBeVisible();
    expect(screen.queryByText("Sign in")).not.toBeInTheDocument();
  });

  it("renders the display name from /me with the restored bearer", async () => {
    let authorization: string | null = null;
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () =>
        HttpResponse.json(tokens),
      ),
      http.get("*/api/v1/identity/me", ({ request }) => {
        authorization = request.headers.get("Authorization");
        return HttpResponse.json(me);
      }),
    );
    mount("/");
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Welcome, Ada Admin",
      }),
    ).toBeVisible();
    expect(authorization).toBe("Bearer access-1");
  });

  it("shows the problem with a retry when /me fails", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () =>
        HttpResponse.json(tokens),
      ),
      http.get("*/api/v1/identity/me", () =>
        HttpResponse.json(
          { type: "urn:dc:problem:unavailable", title: "Down", status: 503 },
          {
            status: 503,
            headers: { "Content-Type": "application/problem+json" },
          },
        ),
      ),
    );
    mount("/");
    expect(
      await screen.findByText("Service temporarily unavailable"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry loading" })).toBeVisible();
  });
});

describe("the sign-in page", () => {
  const user = () => userEvent.setup();

  // Visiting /signin probes the cookie once; with no session that is a 401.
  beforeEach(() => {
    server.use(
      http.post("*/api/v1/identity/auth/refresh", unauthorized),
      // The page offers the methods the public policy leaves on (quick 261120-0ye).
      http.get("*/api/v1/identity/auth/policy", () =>
        HttpResponse.json({
          methods: { local: true, microsoft: true },
          password_rules: {
            min_length: 12,
            require_upper: false,
            require_lower: false,
            require_digit: false,
            require_symbol: false,
          },
        }),
      ),
    );
  });

  it("has the brand panel as an aside with no heading, and one h1", async () => {
    mount("/signin");
    await screen.findByRole("heading", { level: 1, name: "Sign in" });
    const aside = screen.getByRole("complementary", {
      name: "About Contractum",
    });
    expect(aside).toHaveTextContent(
      "Author, version and publish data contracts that pass every governance rule.",
    );
    expect(aside).toHaveTextContent("Data contract governance");
    expect(aside.querySelector("h1,h2,h3")).toBeNull();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(document.title).toBe("Sign in · Contractum");
    expect(await screen.findByLabelText("Username")).toHaveAttribute(
      "autocomplete",
      "username",
    );
    expect(screen.getByLabelText("Password")).toHaveAttribute(
      "autocomplete",
      "current-password",
    );
  });

  it("shows the fixed copy for a known notice", async () => {
    mount("/signin?notice=session-expired");
    expect(
      await screen.findByText("Your session expired. Sign in again."),
    ).toBeVisible();
  });

  it("drops an unknown notice from the URL", async () => {
    mount("/signin?notice=%3Cb%3Ex");
    await screen.findByRole("heading", { level: 1, name: "Sign in" });
    expect(screen.queryByText("Information")).not.toBeInTheDocument();
    expect(screen.queryByText(/<b>x/)).not.toBeInTheDocument();
  });

  it("sends a signed-in user to the redirect target, or home when it is off-origin", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () =>
        HttpResponse.json(tokens),
      ),
      http.get("*/api/v1/identity/me", () => HttpResponse.json(me)),
    );
    const first = mount("/signin?redirect=%2F%3Fx%3D1");
    await screen.findByRole("heading", { level: 1, name: /^Welcome, / });
    expect(first.router.state.location.pathname).toBe("/");
    expect(first.router.state.location.searchStr).toBe("?x=1");
  });

  it("signs in, navigates home and shows the name", async () => {
    let loginHeader: string | null = null;
    server.use(
      http.post("*/api/v1/identity/auth/login", ({ request }) => {
        loginHeader = request.headers.get("X-Token-Delivery");
        return HttpResponse.json(tokens);
      }),
      http.get("*/api/v1/identity/me", () => HttpResponse.json(me)),
    );
    const { router } = mount("/signin");
    const u = user();
    await u.type(await screen.findByLabelText("Username"), "ada");
    await u.type(screen.getByLabelText("Password"), "pw{Enter}");
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Welcome, Ada Admin",
      }),
    ).toBeVisible();
    expect(loginHeader).toBe("cookie");
    expect(router.state.location.pathname).toBe("/");
  });

  it("shows the one generic 401 alert, clears and focuses the password, keeps the username", async () => {
    server.use(http.post("*/api/v1/identity/auth/login", unauthorized));
    mount("/signin");
    const u = user();
    await u.type(await screen.findByLabelText("Username"), "ada");
    await u.type(screen.getByLabelText("Password"), "wrong");
    await u.click(screen.getByRole("button", { name: "Sign in" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Username or password is incorrect.");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByLabelText("Username")).toHaveValue("ada");
    await waitFor(() => {
      expect(screen.getByLabelText("Password")).toHaveFocus();
    });
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("is busy while signing in and ignores a repeat submit", async () => {
    let calls = 0;
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post("*/api/v1/identity/auth/login", async () => {
        calls += 1;
        await gate;
        return unauthorized();
      }),
    );
    mount("/signin");
    const u = user();
    await u.type(await screen.findByLabelText("Username"), "ada");
    await u.type(screen.getByLabelText("Password"), "pw{Enter}");
    const button = await screen.findByRole("button", { name: /Signing in/ });
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveTextContent("Signing in…");
    await u.type(screen.getByLabelText("Password"), "{Enter}");
    await u.click(button);
    release();
    await screen.findByRole("alert");
    expect(calls).toBe(1);
  });

  it("asks for the missing fields and focuses the first one", async () => {
    mount("/signin");
    const u = user();
    await u.click(await screen.findByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Enter your username.")).toBeVisible();
    expect(screen.getByText("Enter your password.")).toBeVisible();
    expect(screen.getByLabelText("Username")).toHaveFocus();
    await u.type(screen.getByLabelText("Username"), "ada");
    await u.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.queryByText("Enter your username.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveFocus();
  });

  it("shows the throttle as a warning, not as a credential error", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/login", () =>
        HttpResponse.json(
          { type: "urn:dc:problem:rate-limited", title: "Slow", status: 429 },
          {
            status: 429,
            headers: { "Content-Type": "application/problem+json" },
          },
        ),
      ),
    );
    mount("/signin");
    const u = user();
    await u.type(await screen.findByLabelText("Username"), "ada");
    await u.type(screen.getByLabelText("Password"), "pw{Enter}");
    expect(
      await screen.findByText("Too many attempts, try again in a moment."),
    ).toBeVisible();
    expect(
      screen.queryByText("Username or password is incorrect."),
    ).not.toBeInTheDocument();
  });

  it("sends a bootstrap one-time password to the rotate screen without signing in", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/login", () =>
        HttpResponse.json({ ...tokens, scope: "password:rotate" }),
      ),
    );
    const { router } = mount("/signin");
    const u = user();
    await u.type(await screen.findByLabelText("Username"), "admin");
    await u.type(screen.getByLabelText("Password"), "one-time{Enter}");
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Set a new password",
      }),
    ).toBeVisible();
    expect(router.state.location.pathname).toBe("/signin/rotate");
    expect(tokenStore.get()).toBeNull();
  });
});

describe("the sign-in page, methods and edge cases", () => {
  const policy = (methods: { local: boolean; microsoft: boolean }) =>
    http.get("*/api/v1/identity/auth/policy", () =>
      HttpResponse.json({
        methods,
        password_rules: {
          min_length: 12,
          require_upper: false,
          require_lower: false,
          require_digit: false,
          require_symbol: false,
        },
      }),
    );

  beforeEach(() => {
    server.use(http.post("*/api/v1/identity/auth/refresh", unauthorized));
  });

  it("says no method is available when the policy turned both off", async () => {
    server.use(policy({ local: false, microsoft: false }));
    mount("/signin", { entraEnabled: true });
    expect(await screen.findByText(/Sign-in is not available/)).toBeVisible();
    expect(screen.queryByLabelText("Username")).toBeNull();
  });

  it("starts Microsoft sign-in on the callback page with the redirect", async () => {
    server.use(policy({ local: true, microsoft: true }));
    const { router } = mount("/signin?redirect=%2Faudit", {
      entraEnabled: true,
    });
    const u = userEvent.setup();
    const microsoft = await screen.findByRole("button", {
      name: "Sign in with Microsoft",
    });
    expect(screen.getByLabelText("Username")).toBeVisible();
    // The callback page would call MSAL; the navigation is recorded, not followed.
    const navigate = vi.spyOn(router, "navigate").mockResolvedValue(undefined);
    await u.click(microsoft);
    expect(navigate).toHaveBeenCalledWith({
      to: "/auth/callback",
      search: { start: "1", redirect: "/audit" },
    });
    expect(
      screen.getByRole("button", { name: "Redirecting…" }),
    ).toBeInTheDocument();
  });

  it("offers only Microsoft, with a notice, when local sign-in is off", async () => {
    server.use(policy({ local: false, microsoft: true }));
    const { router } = mount("/signin?notice=signed-out-elsewhere", {
      entraEnabled: true,
    });
    const microsoft = await screen.findByRole("button", {
      name: "Sign in with Microsoft",
    });
    expect(screen.queryByLabelText("Username")).toBeNull();
    expect(screen.getByText(/signed out in another tab/i)).toBeVisible();
    const navigate = vi.spyOn(router, "navigate").mockResolvedValue(undefined);
    await userEvent.setup().click(microsoft);
    expect(navigate).toHaveBeenCalledWith({
      to: "/auth/callback",
      search: { start: "1" },
    });
  });

  it("still shows the page when the cookie probe fails", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/refresh", () =>
        HttpResponse.json(
          { type: "urn:dc:problem:unavailable", title: "Down", status: 503 },
          {
            status: 503,
            headers: { "Content-Type": "application/problem+json" },
          },
        ),
      ),
    );
    const { router } = mount("/signin");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Sign in" }),
    ).toBeVisible();
    expect(router.state.location.pathname).toBe("/signin");
  });

  it("uses the configured methods and today's rules when the policy has no body", async () => {
    server.use(
      http.get(
        "*/api/v1/identity/auth/policy",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );
    mount("/signin");
    expect(await screen.findByLabelText("Username")).toBeVisible();
  });

  it("keeps the redirect when a rotate visit without a one-time session restarts sign-in", async () => {
    server.use(policy({ local: true, microsoft: false }));
    const { router } = mount("/signin/rotate?redirect=%2Fcards");
    await screen.findByRole("heading", { level: 1, name: "Sign in" });
    expect(router.state.location.search).toEqual({
      notice: "rotation-restart",
      redirect: "/cards",
    });
  });

  it("ignores a second submit event while the first sign-in is in flight", async () => {
    server.use(policy({ local: true, microsoft: false }));
    let calls = 0;
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post("*/api/v1/identity/auth/login", async () => {
        calls += 1;
        await gate;
        return unauthorized();
      }),
    );
    mount("/signin");
    const u = userEvent.setup();
    await u.type(await screen.findByLabelText("Username"), "ada");
    await u.type(screen.getByLabelText("Password"), "pw{Enter}");
    const busy = await screen.findByRole("button", { name: /Signing in/ });
    const form = busy.closest("form");
    if (form === null) throw new Error("the sign-in form is missing");
    fireEvent.submit(form);
    release();
    await screen.findByRole("alert");
    expect(calls).toBe(1);
  });
});
