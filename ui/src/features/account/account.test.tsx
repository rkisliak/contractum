// /account/password through the real router: the loading, failed and Microsoft-managed states,
// client-side checks with focus on the first invalid field, the body sent, a wrong current
// password, mapped and unmapped 422 errors, a server failure and the sign-out on success.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeMe, renderApp } from "@/test/renderApp";
import { checkDisplayName } from "./ProfilePage";

afterEach(() => {
  tokenStore.clear();
});

const PASSWORD_PATH = "*/api/v1/identity/auth/password";
const SLOW = { timeout: 10000 };

function problem(
  status: number,
  slug: string,
  detail: string,
  errors?: { pointer: string; detail: string }[],
) {
  return HttpResponse.json(
    {
      type: `urn:dc:problem:${slug}`,
      title: "Refused",
      status,
      detail,
      ...(errors ? { errors } : {}),
    },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

async function findForm() {
  return within(
    (
      await screen.findByRole("button", { name: "Change password" }, SLOW)
    ).closest("form") as HTMLFormElement,
  );
}

async function fill(
  actor: ReturnType<typeof userEvent.setup>,
  form: Awaited<ReturnType<typeof findForm>>,
  values: { current?: string; next?: string; confirm?: string },
) {
  if (values.current)
    await actor.type(form.getByLabelText("Current password"), values.current);
  if (values.next)
    await actor.type(form.getByLabelText("New password"), values.next);
  if (values.confirm)
    await actor.type(
      form.getByLabelText("Confirm new password"),
      values.confirm,
    );
}

describe("change password", () => {
  it("shows a skeleton while /me loads", async () => {
    renderApp("/account/password", {
      handlers: [
        http.get("*/api/v1/identity/me", () => new Promise<never>(() => {})),
      ],
    });
    expect(
      await screen.findByText("Loading your account", {}, SLOW),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "Change password" }),
    ).toBeVisible();
    expect(screen.queryByLabelText("Current password")).toBeNull();
  });

  it("renders a failed /me as a ProblemAlert and retries it", async () => {
    const actor = userEvent.setup();
    let calls = 0;
    renderApp("/account/password", {
      handlers: [
        http.get("*/api/v1/identity/me", () => {
          calls += 1;
          return problem(500, "internal", "Something broke.");
        }),
      ],
    });
    const retry = await screen.findByRole(
      "button",
      { name: "Retry loading" },
      SLOW,
    );
    expect(screen.queryByLabelText("Current password")).toBeNull();
    const before = calls;
    await actor.click(retry);
    await waitFor(() => {
      expect(calls).toBeGreaterThan(before);
    });
  });

  it("tells an Entra user that Microsoft manages the password", async () => {
    renderApp("/account/password", { me: makeMe({ idp: "entra" }) });
    expect(
      await screen.findByText(
        /Your password is managed by Microsoft/,
        {},
        SLOW,
      ),
    ).toBeVisible();
    expect(screen.queryByLabelText("Current password")).toBeNull();
    // The account tabs still render.
    expect(
      screen.getByRole("navigation", { name: "Account settings" }),
    ).toBeVisible();
  });

  it("shows the same notice to a non-interactive principal", async () => {
    renderApp("/account/password", { me: makeMe({ interactive: false }) });
    expect(
      await screen.findByText(
        /Your password is managed by Microsoft/,
        {},
        SLOW,
      ),
    ).toBeVisible();
  });

  it("checks every field before sending and focuses the first invalid one", async () => {
    const actor = userEvent.setup();
    let posted = false;
    renderApp("/account/password", {
      handlers: [
        http.post(PASSWORD_PATH, () => {
          posted = true;
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    const form = await findForm();
    await actor.click(form.getByRole("button", { name: "Change password" }));
    expect(form.getByText("Enter your current password.")).toBeVisible();
    expect(form.getByText("Use at least 12 characters.")).toBeVisible();
    expect(form.getByLabelText("Current password")).toHaveFocus();

    await fill(actor, form, { current: "old-secret" });
    await actor.click(form.getByRole("button", { name: "Change password" }));
    expect(form.queryByText("Enter your current password.")).toBeNull();
    expect(form.getByLabelText("New password")).toHaveFocus();

    await fill(actor, form, {
      next: "a-long-passphrase-42",
      confirm: "a-long-passphrase-43",
    });
    await actor.click(form.getByRole("button", { name: "Change password" }));
    expect(form.getByText("Passwords do not match.")).toBeVisible();
    expect(form.getByLabelText("Confirm new password")).toHaveFocus();
    expect(posted).toBe(false);
  });

  it("posts both passwords, clears the token and signs out with the notice", async () => {
    const actor = userEvent.setup();
    let body: unknown = null;
    const { session } = renderApp("/account/password", {
      handlers: [
        http.post(PASSWORD_PATH, async ({ request }) => {
          body = await request.json();
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    const redirect = vi.spyOn(session, "end");
    const form = await findForm();
    expect(tokenStore.get()).not.toBeNull();
    await fill(actor, form, {
      current: "old-secret",
      next: "a-long-passphrase-42",
      confirm: "a-long-passphrase-42",
    });
    await actor.click(form.getByRole("button", { name: "Change password" }));
    await waitFor(() => {
      expect(redirect).toHaveBeenCalledWith("password-changed");
    });
    expect(body).toEqual({
      current_password: "old-secret",
      new_password: "a-long-passphrase-42",
    });
    expect(tokenStore.get()).toBeNull();
  });

  it("clears and focuses the current password after a 401", async () => {
    const actor = userEvent.setup();
    const { session } = renderApp("/account/password", {
      handlers: [
        http.post(PASSWORD_PATH, () =>
          problem(401, "invalid-credentials", "Wrong password."),
        ),
      ],
    });
    const redirect = vi.spyOn(session, "end");
    const form = await findForm();
    await fill(actor, form, {
      current: "wrong-secret",
      next: "a-long-passphrase-42",
      confirm: "a-long-passphrase-42",
    });
    await actor.click(form.getByRole("button", { name: "Change password" }));
    expect(
      await form.findByText("Current password is incorrect."),
    ).toBeVisible();
    expect(form.getByLabelText("Current password")).toHaveValue("");
    expect(form.getByLabelText("Current password")).toHaveFocus();
    expect(form.getByLabelText("New password")).toHaveValue(
      "a-long-passphrase-42",
    );
    expect(redirect).not.toHaveBeenCalled();
    // A field error, not a ProblemAlert with the slug.
    expect(screen.queryByText("invalid-credentials")).toBeNull();
  });

  it("puts a mapped 422 pointer on its field without an alert", async () => {
    const actor = userEvent.setup();
    renderApp("/account/password", {
      handlers: [
        http.post(PASSWORD_PATH, () =>
          problem(422, "validation-failed", "Invalid.", [
            {
              pointer: "/new_password",
              detail: "This password appears in a breach list.",
            },
          ]),
        ),
      ],
    });
    const form = await findForm();
    await fill(actor, form, {
      current: "old-secret",
      next: "password1234",
      confirm: "password1234",
    });
    await actor.click(form.getByRole("button", { name: "Change password" }));
    expect(
      await form.findByText("This password appears in a breach list."),
    ).toBeVisible();
    expect(form.queryByText("validation-failed")).toBeNull();
  });

  it("lists an unmapped 422 pointer in a ProblemAlert", async () => {
    const actor = userEvent.setup();
    renderApp("/account/password", {
      handlers: [
        http.post(PASSWORD_PATH, () =>
          problem(422, "validation-failed", "Invalid request.", [
            { pointer: "/current_password", detail: "Too short." },
            { pointer: "/other", detail: "Unexpected field." },
          ]),
        ),
      ],
    });
    const form = await findForm();
    await fill(actor, form, {
      current: "old-secret",
      next: "a-long-passphrase-42",
      confirm: "a-long-passphrase-42",
    });
    await actor.click(form.getByRole("button", { name: "Change password" }));
    expect(await form.findByText(/Unexpected field\./)).toBeVisible();
    // A problem the user can fix keeps the slug in the collapsed technical details.
    expect(form.getByText("validation-failed")).not.toBeVisible();
    await actor.click(form.getByText("Technical details"));
    expect(form.getByText("validation-failed")).toBeVisible();
    expect(form.getByText(/Unexpected field\./)).toBeVisible();
    // The mapped one still lands on its field.
    expect(document.getElementById("current-password-error")).toHaveTextContent(
      "Too short.",
    );
  });

  it("shows a server failure in a ProblemAlert and clears it on resubmit", async () => {
    const actor = userEvent.setup();
    let fail = true;
    const { session } = renderApp("/account/password", {
      handlers: [
        http.post(PASSWORD_PATH, () =>
          fail
            ? problem(409, "conflict", "Password was changed elsewhere.")
            : new HttpResponse(null, { status: 204 }),
        ),
      ],
    });
    const redirect = vi.spyOn(session, "end");
    const form = await findForm();
    await fill(actor, form, {
      current: "old-secret",
      next: "a-long-passphrase-42",
      confirm: "a-long-passphrase-42",
    });
    await actor.click(form.getByRole("button", { name: "Change password" }));
    expect(
      await form.findByText("Password was changed elsewhere."),
    ).toBeVisible();
    expect(form.getByText("conflict")).not.toBeVisible();
    fail = false;
    await actor.click(form.getByRole("button", { name: "Change password" }));
    await waitFor(() => {
      expect(redirect).toHaveBeenCalledWith("password-changed");
    });
    expect(form.queryByText("Password was changed elsewhere.")).toBeNull();
  });
});

describe("profile", () => {
  const ME_PATH = "*/api/v1/identity/me";

  it("shows the Profile card with the current name and the three account tabs", async () => {
    renderApp("/account/profile");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Profile" }, SLOW),
    ).toBeVisible();
    expect(await screen.findByLabelText("Display name")).toHaveValue(
      "Ada Admin",
    );
    const nav = screen.getByRole("navigation", { name: "Account settings" });
    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Profile", "Password", "Access tokens"]);
  });

  it("sends the trimmed name, refetches /me and shows the new name", async () => {
    const actor = userEvent.setup();
    let name = "Ada Admin";
    let body: unknown = null;
    renderApp("/account/profile", {
      handlers: [
        http.get(ME_PATH, () =>
          HttpResponse.json(makeMe({ display_name: name })),
        ),
        http.patch(ME_PATH, async ({ request }) => {
          body = await request.json();
          name = (body as { display_name: string }).display_name;
          return HttpResponse.json({
            principal_id: "local:11111111-1111-4111-8111-111111111111",
            display_name: name,
            username: "ada",
            updated_at: "2026-11-19T10:00:00Z",
          });
        }),
      ],
    });
    const field = await screen.findByLabelText("Display name", {}, SLOW);
    await waitFor(() => {
      expect(field).toHaveValue("Ada Admin");
    });
    await actor.clear(field);
    await actor.type(field, "  Ada Lovelace  ");
    await actor.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(body).toEqual({ display_name: "Ada Lovelace" });
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Display name")).toHaveValue("Ada Lovelace");
    });
  });

  it("refuses an empty name before sending", async () => {
    const actor = userEvent.setup();
    let patched = false;
    renderApp("/account/profile", {
      handlers: [
        http.patch(ME_PATH, () => {
          patched = true;
          return new HttpResponse(null, { status: 500 });
        }),
      ],
    });
    const field = await screen.findByLabelText("Display name", {}, SLOW);
    await waitFor(() => {
      expect(field).toHaveValue("Ada Admin");
    });
    await actor.clear(field);
    await actor.type(field, "   ");
    await actor.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Enter a display name.")).toBeVisible();
    expect(patched).toBe(false);
  });

  it("shows a /display_name 422 on the field", async () => {
    const actor = userEvent.setup();
    renderApp("/account/profile", {
      handlers: [
        http.patch(ME_PATH, () =>
          problem(422, "validation-error", "The request failed validation.", [
            {
              pointer: "/display_name",
              detail:
                "control or bidirectional formatting characters are not allowed",
            },
          ]),
        ),
      ],
    });
    const field = await screen.findByLabelText("Display name", {}, SLOW);
    await waitFor(() => {
      expect(field).toHaveValue("Ada Admin");
    });
    await actor.type(field, " Jr");
    await actor.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(
        "control or bidirectional formatting characters are not allowed",
      ),
    ).toBeVisible();
  });

  it("shows an Entra user the name read-only with the reason and no Save", async () => {
    renderApp("/account/profile", {
      me: makeMe({ idp: "entra", display_name: "Eve Entra" }),
    });
    const field = await screen.findByLabelText("Display name", {}, SLOW);
    expect(field).toHaveValue("Eve Entra");
    expect(field).toHaveAttribute("readonly");
    expect(
      screen.getByText("Your name comes from Microsoft Entra ID"),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("explains a non-interactive credential cannot change the name", async () => {
    renderApp("/account/profile", { me: makeMe({ interactive: false }) });
    expect(
      await screen.findByText(
        "Sign in interactively to change your display name.",
        {},
        SLOW,
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });
});

describe("the name rule on the profile", () => {
  it("shows the message for an emoji and sends nothing", async () => {
    const actor = userEvent.setup();
    let patched = false;
    renderApp("/account/profile", {
      handlers: [
        http.patch("*/api/v1/identity/me", () => {
          patched = true;
          return HttpResponse.json({});
        }),
      ],
    });
    const field = await screen.findByLabelText("Display name", {}, SLOW);
    await waitFor(() => {
      expect(field).toHaveValue("Ada Admin");
    });
    await actor.type(field, " 😀");
    expect(
      screen.getByText(
        "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.",
      ),
    ).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Save" }));
    expect(patched).toBe(false);
  });
});

describe("profile, more states", () => {
  it("saves the unchanged current name and shows a server failure in the alert", async () => {
    const actor = userEvent.setup();
    let body: unknown = null;
    renderApp("/account/profile", {
      handlers: [
        http.patch("*/api/v1/identity/me", async ({ request }) => {
          body = await request.json();
          return problem(500, "internal", "The name could not be saved.");
        }),
      ],
    });
    await screen.findByLabelText("Display name", {}, SLOW);
    await actor.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText("The name could not be saved."),
    ).toBeVisible();
    expect(body).toEqual({ display_name: "Ada Admin" });
  });
});

describe("checkDisplayName", () => {
  it("accepts a name of at most 200 characters and refuses a longer one", () => {
    expect(checkDisplayName("x".repeat(200))).toBeUndefined();
    expect(checkDisplayName("x".repeat(201))).toBe(
      "Use at most 200 characters.",
    );
    expect(checkDisplayName("   ")).toBe("Enter a display name.");
  });
});
