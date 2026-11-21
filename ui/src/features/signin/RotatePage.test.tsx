// The forced bootstrap rotation (UI-SPEC 7.3) through the real route tree, router, session and
// API client, with msw standing in for the gateway.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createApiClient } from "@/api/client";
import { createSession, type Session } from "@/auth/session";
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

const restricted = {
  access_token: "restricted-1",
  expires_in: 300,
  scope: "password:rotate",
  token_type: "Bearer",
};
const full = {
  ...restricted,
  access_token: "access-1",
  expires_in: 900,
  scope: "",
};

const me = {
  principal_id: "11111111-1111-4111-8111-111111111111",
  principal_type: "user",
  idp: "local",
  via: "jwt",
  display_name: "Admin",
  username: "admin",
  preferred_username: null,
  read_only: false,
  interactive: true,
  groups: [],
  assignments: [],
  global_permissions: [],
};

const NEW_PASSWORD = "a-long-passphrase-of-24c";

function problem(status: number, body: Record<string, unknown>) {
  return HttpResponse.json(
    { status, title: "Refused", ...body },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

type Recorded = { auth: string | null; body: unknown };

function mount(path: string, existing?: Session) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const api = createApiClient(config);
  const session = existing ?? createSession(api, { navigate: () => undefined });
  const context = { config, queryClient, api, session };
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
  return { router, session };
}

let logins: Recorded[];
let rotations: Recorded[];

beforeEach(() => {
  logins = [];
  rotations = [];
  server.use(
    http.post("*/api/v1/identity/auth/refresh", () =>
      problem(401, { type: "urn:dc:problem:invalid-token" }),
    ),
    http.post("*/api/v1/identity/auth/login", async ({ request }) => {
      const body = (await request.json()) as { password: string };
      logins.push({ auth: request.headers.get("X-Token-Delivery"), body });
      return HttpResponse.json(
        body.password === NEW_PASSWORD ? full : restricted,
      );
    }),
    http.post("*/api/v1/identity/auth/password", async ({ request }) => {
      rotations.push({
        auth: request.headers.get("Authorization"),
        body: await request.json(),
      });
      return new HttpResponse(null, { status: 204 });
    }),
    http.get("*/api/v1/identity/me", () => HttpResponse.json(me)),
    http.get("*/api/v1/identity/me/capabilities", () =>
      HttpResponse.json({ folder_id: null, permissions: [] }),
    ),
    http.get("*/api/v1/identity/folders", () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    ),
  );
});

afterEach(() => {
  tokenStore.clear();
});

async function reachRotate(path = "/signin") {
  const mounted = mount(path);
  const u = userEvent.setup();
  await u.type(await screen.findByLabelText("Username"), "admin");
  await u.type(screen.getByLabelText("Password"), "one-time-secret{Enter}");
  await screen.findByRole("heading", { level: 1, name: "Set a new password" });
  return { ...mounted, u };
}

describe("the rotate screen", () => {
  it("rotates with the restricted token, signs in again and lands home with a toast", async () => {
    const { router, u } = await reachRotate();
    expect(
      screen.getByText(
        "Your account was created with a one-time password. Choose a new password to continue.",
      ),
    ).toBeVisible();
    await u.type(screen.getByLabelText("New password"), NEW_PASSWORD);
    await u.type(screen.getByLabelText("Confirm new password"), NEW_PASSWORD);
    await u.click(screen.getByRole("button", { name: "Set new password" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "Welcome, Admin" }),
    ).toBeVisible();
    expect(router.state.location.pathname).toBe("/");
    expect(
      await screen.findByText("Password set. You are signed in."),
    ).toBeVisible();
    expect(rotations).toEqual([
      { auth: "Bearer restricted-1", body: { new_password: NEW_PASSWORD } },
    ]);
    expect(logins).toHaveLength(2);
    expect(logins[1]).toEqual({
      auth: "cookie",
      body: { username: "admin", password: NEW_PASSWORD },
    });
    expect(tokenStore.get()?.token).toBe("access-1");
  });

  it("follows the redirect it was given", async () => {
    const { router, u } = await reachRotate(
      "/signin?redirect=%2Faccount%2Fpassword",
    );
    expect(router.state.location.search).toEqual({
      redirect: "/account/password",
    });
    await u.type(screen.getByLabelText("New password"), NEW_PASSWORD);
    await u.type(screen.getByLabelText("Confirm new password"), NEW_PASSWORD);
    await u.click(screen.getByRole("button", { name: "Set new password" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/account/password");
    });
  });

  it("sends a reload without a restricted session back to sign-in", async () => {
    const { router } = mount("/signin/rotate");
    expect(
      await screen.findByText(
        "Sign in with your one-time password again to set a new password.",
      ),
    ).toBeVisible();
    expect(router.state.location.pathname).toBe("/signin");
    expect(router.state.location.search).toEqual({
      notice: "rotation-restart",
    });
  });

  it("validates length and confirmation before calling the API", async () => {
    const { u } = await reachRotate();
    await u.type(screen.getByLabelText("New password"), "short-pass1");
    await u.type(screen.getByLabelText("Confirm new password"), "different");
    await u.click(screen.getByRole("button", { name: "Set new password" }));
    expect(screen.getByText("Use at least 12 characters.")).toBeVisible();
    expect(screen.getByLabelText("New password")).toHaveFocus();
    expect(screen.getByText("Passwords do not match.")).toBeVisible();
    // Per-field after the first submit.
    await u.type(screen.getByLabelText("New password"), "-longer");
    expect(
      screen.queryByText("Use at least 12 characters."),
    ).not.toBeInTheDocument();
    expect(rotations).toHaveLength(0);
  });

  it("puts a policy refusal on the new-password field and others in the alert", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/password", () =>
        problem(422, {
          type: "urn:dc:problem:validation-error",
          errors: [
            {
              pointer: "/new_password",
              detail: "This password is too common.",
            },
            { pointer: "/other", detail: "Something else is wrong." },
          ],
        }),
      ),
    );
    const { u } = await reachRotate();
    await u.type(screen.getByLabelText("New password"), NEW_PASSWORD);
    await u.type(screen.getByLabelText("Confirm new password"), NEW_PASSWORD);
    await u.click(screen.getByRole("button", { name: "Set new password" }));
    expect(
      await screen.findByText("This password is too common."),
    ).toBeVisible();
    expect(screen.getByLabelText("New password")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByText(/Something else is wrong\./)).toBeVisible();
    expect(tokenStore.get()).toBeNull();
  });

  it("shows a consumed bootstrap as a problem alert", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/password", () =>
        problem(409, {
          type: "urn:dc:problem:bootstrap-consumed",
          title: "Bootstrap already consumed",
        }),
      ),
    );
    const { u } = await reachRotate();
    await u.type(screen.getByLabelText("New password"), NEW_PASSWORD);
    await u.type(screen.getByLabelText("Confirm new password"), NEW_PASSWORD);
    await u.click(screen.getByRole("button", { name: "Set new password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Bootstrap already consumed",
    );
  });

  it("sends one request for a double submit and shows Saving…", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post("*/api/v1/identity/auth/password", async ({ request }) => {
        rotations.push({ auth: null, body: await request.json() });
        await gate;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { u } = await reachRotate();
    await u.type(screen.getByLabelText("New password"), NEW_PASSWORD);
    await u.type(screen.getByLabelText("Confirm new password"), NEW_PASSWORD);
    await u.click(screen.getByRole("button", { name: "Set new password" }));
    const busy = await screen.findByRole("button", { name: "Saving…" });
    expect(busy).toHaveAttribute("aria-busy", "true");
    await u.click(busy);
    release();
    await screen.findByRole("heading", { level: 1, name: "Welcome, Admin" });
    expect(rotations).toHaveLength(1);
  });

  it("goes back to sign-in and forgets the restricted token", async () => {
    const { router, session, u } = await reachRotate();
    expect(session.restrictedToken()).toBe("restricted-1");
    await u.click(screen.getByRole("button", { name: "Back to sign in" }));
    expect(session.restrictedToken()).toBeNull();
    await screen.findByRole("heading", { level: 1, name: "Sign in" });
    expect(router.state.location.pathname).toBe("/signin");
  });
});

describe("the rotate screen, other outcomes", () => {
  async function submitNew(u: ReturnType<typeof userEvent.setup>) {
    await u.type(screen.getByLabelText("New password"), NEW_PASSWORD);
    await u.type(screen.getByLabelText("Confirm new password"), NEW_PASSWORD);
    await u.click(screen.getByRole("button", { name: "Set new password" }));
  }

  it("focuses the confirmation when only it is wrong", async () => {
    const { u } = await reachRotate();
    await u.type(screen.getByLabelText("New password"), NEW_PASSWORD);
    await u.type(screen.getByLabelText("Confirm new password"), "other");
    await u.click(screen.getByRole("button", { name: "Set new password" }));
    expect(screen.getByText("Passwords do not match.")).toBeVisible();
    expect(screen.getByLabelText("Confirm new password")).toHaveFocus();
    expect(rotations).toHaveLength(0);
  });

  it("restarts sign-in, keeping the redirect, when the restricted token expired", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/password", () =>
        problem(401, { type: "urn:dc:problem:invalid-token" }),
      ),
    );
    const { router, session, u } = await reachRotate(
      "/signin?redirect=%2Fcards",
    );
    await submitNew(u);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/signin");
    });
    expect(router.state.location.search).toEqual({
      redirect: "/cards",
      notice: "rotation-restart",
    });
    expect(session.restrictedToken()).toBeNull();
  });

  it("restarts sign-in when the new password is itself one-time", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/login", () =>
        HttpResponse.json(restricted),
      ),
    );
    const { router, u } = await reachRotate();
    await submitNew(u);
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        notice: "rotation-restart",
      });
    });
    expect(router.state.location.pathname).toBe("/signin");
  });

  it("shows a refusal only on the field when nothing else was refused", async () => {
    server.use(
      http.post("*/api/v1/identity/auth/password", () =>
        problem(422, {
          type: "urn:dc:problem:validation-error",
          errors: [{ pointer: "/new_password", detail: "Too common." }],
        }),
      ),
    );
    const { u } = await reachRotate();
    await submitNew(u);
    expect(await screen.findByText("Too common.")).toBeVisible();
    expect(screen.getByLabelText("New password")).toHaveFocus();
    // The field's own error is the only alert: no ProblemAlert repeats it.
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByRole("alert")).toHaveTextContent("Too common.");
  });

  it("ignores a second submit event while the first is in flight", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post("*/api/v1/identity/auth/password", async ({ request }) => {
        rotations.push({ auth: null, body: await request.json() });
        await gate;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { u } = await reachRotate();
    await submitNew(u);
    const busy = await screen.findByRole("button", { name: "Saving…" });
    const form = busy.closest("form");
    if (form === null) throw new Error("the rotate form is missing");
    fireEvent.submit(form);
    release();
    await screen.findByRole("heading", { level: 1, name: "Welcome, Admin" });
    expect(rotations).toHaveLength(1);
  });
});
