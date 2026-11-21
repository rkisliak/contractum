// Admin > Settings (quick 261120-0yc): the page, the Entra and Platform tabs, the banner, export
// and import, the non-admin refusal and the Register pre-fill. The API is stubbed with msw.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeMe, renderApp, ROOT_ID } from "@/test/renderApp";
import {
  admin,
  folders,
  LOAD,
  noBody,
  OTHER,
  overviewBody,
  problem,
  section,
  SETTINGS,
  stack,
  TENANT,
} from "@/test/settings-fixtures";

afterEach(() => {
  tokenStore.clear();
  window.sessionStorage.clear();
});

describe("the Settings page", () => {
  beforeEach(() => {
    tokenStore.clear();
  });

  it("shows a Global admin the header, both tabs and the page actions", async () => {
    renderApp("/admin/settings", { me: admin, folders, handlers: stack() });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Settings" }, LOAD),
    ).toBeVisible();
    expect(
      await screen.findByRole("tab", { name: "Entra" }, LOAD),
    ).toBeVisible();
    expect(screen.getByRole("tab", { name: "Platform" })).toBeVisible();
    expect(screen.queryByRole("tab", { name: "Notifications" })).toBeNull();
    expect(screen.getByRole("button", { name: "Export" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Import" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Settings" })).toBeVisible();
  });

  it("renders NotAvailable and makes no settings request without settings:read", async () => {
    const calls: string[] = [];
    renderApp("/admin/settings", {
      me: makeMe({ global_permissions: ["audit:list"] }),
      folders,
      handlers: [
        http.get(`${SETTINGS}/*`, ({ request }) => {
          calls.push(request.url);
          return HttpResponse.json({});
        }),
        http.get(SETTINGS, ({ request }) => {
          calls.push(request.url);
          return HttpResponse.json({});
        }),
      ],
    });
    expect(
      await screen.findByText("Not available at your access level", {}, LOAD),
    ).toBeVisible();
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
    // The shell's own public read is the only settings call a non-admin makes (default handler).
    expect(calls.filter((url) => !url.endsWith("/public"))).toEqual([]);
  });

  it("falls back to the first tab for an unavailable tab", async () => {
    renderApp("/admin/settings?tab=governance", {
      me: admin,
      folders,
      handlers: stack(),
    });
    expect(
      await screen.findByRole("tab", { name: "Entra", selected: true }, LOAD),
    ).toBeVisible();
  });
});

describe("the Entra tab", () => {
  it("saves the default tenant with If-Match and shows the default beside the field", async () => {
    const actor = userEvent.setup();
    let seen: { etag: string | null; body: unknown } | null = null;
    renderApp("/admin/settings?tab=entra", {
      me: admin,
      folders,
      handlers: [
        http.put(`${SETTINGS}/:section`, async ({ request }) => {
          seen = {
            etag: request.headers.get("if-match"),
            body: await request.json(),
          };
          return HttpResponse.json(
            section("entra", { default_tenant_id: OTHER }, 2),
          );
        }),
        ...stack(),
      ],
    });
    const select = await screen.findByLabelText("Default tenant", {}, LOAD);
    expect(screen.getAllByText(/Default:/)[0]).toHaveTextContent(TENANT);
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Use the platform default", TENANT, OTHER]);
    await actor.selectOptions(select, OTHER);
    await actor.click(
      screen.getByRole("button", { name: "Save Entra defaults" }),
    );
    await waitFor(() => {
      expect(seen).toEqual({
        etag: '"entra-r1"',
        body: { default_tenant_id: OTHER },
      });
    });
    expect(await screen.findByText("Entra defaults saved.")).toBeVisible();
  });

  it("resets a stored field to its default by clearing the override", async () => {
    const actor = userEvent.setup();
    let body: unknown = null;
    renderApp("/admin/settings?tab=entra", {
      me: admin,
      folders,
      handlers: [
        http.put(`${SETTINGS}/:section`, async ({ request }) => {
          body = await request.json();
          return HttpResponse.json(section("entra", {}, 2));
        }),
        ...stack({ default_tenant_id: OTHER }),
      ],
    });
    await screen.findByLabelText("Default tenant", {}, LOAD);
    await actor.click(
      screen.getByRole("button", {
        name: "Reset to default for default tenant",
      }),
    );
    await waitFor(() => {
      expect(body).toEqual({});
    });
  });

  it("shows 'changed elsewhere' with a reload on a 412", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/settings?tab=entra", {
      me: admin,
      folders,
      handlers: [
        http.put(`${SETTINGS}/:section`, () =>
          problem(412, "precondition-failed"),
        ),
        ...stack(),
      ],
    });
    await screen.findByLabelText("Default tenant", {}, LOAD);
    await actor.click(
      screen.getByRole("button", { name: "Save Entra defaults" }),
    );
    expect(
      await screen.findByText("These settings changed elsewhere"),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Reload settings" }),
    ).toBeVisible();
  });

  it("puts a 422 pointer on the field", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/settings?tab=entra", {
      me: admin,
      folders,
      handlers: [
        http.put(`${SETTINGS}/:section`, () =>
          problem(422, "validation-error", {
            errors: [
              {
                pointer: "/default_tenant_id",
                detail: "This is not a configured Entra tenant.",
              },
            ],
          }),
        ),
        ...stack(),
      ],
    });
    await screen.findByLabelText("Default tenant", {}, LOAD);
    await actor.click(
      screen.getByRole("button", { name: "Save Entra defaults" }),
    );
    expect(
      await screen.findByText("This is not a configured Entra tenant."),
    ).toBeVisible();
  });

  it("offers only ROOT and Organizations as the default owner", async () => {
    renderApp("/admin/settings?tab=entra", {
      me: admin,
      folders,
      handlers: stack(),
    });
    const owner = await screen.findByLabelText(
      "Default owner for registered Entra identities",
      {},
      LOAD,
    );
    const labels = within(owner)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(labels[0]).toBe("No default owner");
    expect(
      screen.getByText(
        /^Pre-fills the Owner field when you register an Entra user, group or service principal\./,
      ),
    ).toBeVisible();
    expect(labels.some((l) => l.includes("Acme"))).toBe(true);
    expect(labels.some((l) => l.includes("Team"))).toBe(false);
  });

  it("shows the read-only overview and secret metadata without a secret", async () => {
    renderApp("/admin/settings?tab=entra", {
      me: admin,
      folders,
      handlers: stack(),
    });
    expect(
      await screen.findByText("Managed in Helm (view only)", {}, LOAD),
    ).toBeVisible();
    expect(await screen.findByText("Secret file present")).toBeVisible();
    expect(screen.getByText(/900 days left/)).toBeVisible();
    expect(screen.queryByText(/Rotate the Graph client secret/)).toBeNull();
  });

  it("warns to rotate when the expiry is unknown or under 30 days", async () => {
    renderApp("/admin/settings?tab=entra", {
      me: admin,
      folders,
      handlers: [
        http.get(`${SETTINGS}/entra/overview`, () =>
          HttpResponse.json({
            ...overviewBody,
            secret_expiry: {
              status: "unknown",
              expires_at: null,
              days_left: null,
              reason: "Graph refused.",
            },
          }),
        ),
        ...stack(),
      ],
    });
    expect(
      await screen.findByText(
        /Rotate the Graph client secret before it expires/,
        {},
        LOAD,
      ),
    ).toBeVisible();
    expect(screen.getByText("Unknown")).toBeVisible();
  });

  it("tests the connection and words each result", async () => {
    const actor = userEvent.setup();
    let result = "ok";
    renderApp("/admin/settings?tab=entra", {
      me: admin,
      folders,
      handlers: [
        http.post(`${SETTINGS}/entra/test`, () =>
          HttpResponse.json({
            result,
            tenant_id: TENANT,
            checked_at: "2026-11-20T10:00:00Z",
          }),
        ),
        ...stack(),
      ],
    });
    const button = await screen.findByRole(
      "button",
      { name: "Test connection" },
      LOAD,
    );
    await actor.click(button);
    expect(
      await screen.findByText("Connected: Microsoft Graph answered."),
    ).toBeVisible();
    result = "token-refused";
    await actor.click(screen.getByRole("button", { name: "Test connection" }));
    expect(
      await screen.findByText(/refused the Graph client credentials/),
    ).toBeVisible();
  });

  it("answers a 429 on the test with a warning, never a red alert", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/settings?tab=entra", {
      me: admin,
      folders,
      handlers: [
        http.post(`${SETTINGS}/entra/test`, () =>
          problem(429, "too-many-attempts"),
        ),
        ...stack(),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Test connection" }, LOAD),
    );
    expect(await screen.findByText("Too many tests")).toBeVisible();
  });
});

describe("the Platform tab", () => {
  it("saves the banner with a preview, converting the window to UTC", async () => {
    const actor = userEvent.setup();
    let body: Record<string, unknown> | null = null;
    renderApp("/admin/settings?tab=platform", {
      me: admin,
      folders,
      handlers: [
        http.put(`${SETTINGS}/:section`, async ({ request }) => {
          body = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(section("platform", {}, 3));
        }),
        ...stack(),
      ],
    });
    const text = await screen.findByLabelText("Banner text", {}, LOAD);
    await actor.type(text, "Maintenance <b>tonight</b>");
    expect(screen.getByText("26 / 500 characters")).toBeVisible();
    // The preview is plain text: the tags are visible characters, never markup.
    expect(
      screen.getAllByText("Maintenance <b>tonight</b>").length,
    ).toBeGreaterThan(0);
    expect(document.querySelector("main b")).toBeNull();
    await actor.click(screen.getByRole("button", { name: "Warning" }));
    await actor.click(screen.getByRole("button", { name: "Save banner" }));
    await waitFor(() => {
      expect(body).toEqual({
        banner: { text: "Maintenance <b>tonight</b>", severity: "warning" },
      });
    });
  });

  it("refuses an empty text and an end before the start before sending", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/settings?tab=platform", {
      me: admin,
      folders,
      handlers: stack(),
    });
    await screen.findByLabelText("Banner text", {}, LOAD);
    await actor.click(screen.getByRole("button", { name: "Save banner" }));
    expect(
      await screen.findByText(/Enter the announcement text/),
    ).toBeVisible();
  });

  it("lists About as a table with the UI build and an Unreachable service", async () => {
    renderApp("/admin/settings?tab=platform", {
      me: admin,
      folders,
      handlers: stack(),
    });
    const table = await screen.findByRole("table", { name: "Services" }, LOAD);
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(7); // header, ui and five services
    expect(within(table).getByRole("rowheader", { name: "ui" })).toBeVisible();
    expect(within(table).getByText("Unreachable")).toBeVisible();
    expect(within(table).getAllByText("OK")).toHaveLength(5);
    expect(within(table).getByText("0011_platform_settings")).toBeVisible();
  });
});

describe("export and import", () => {
  it("shows the dry-run diff and applies with the combined etag", async () => {
    const actor = userEvent.setup();
    const calls: { dry: string | null; etag: string | null }[] = [];
    renderApp("/admin/settings", {
      me: admin,
      folders,
      handlers: [
        http.post(`${SETTINGS}/import`, ({ request }) => {
          calls.push({
            dry: new URL(request.url).searchParams.get("dry_run"),
            etag: request.headers.get("if-match"),
          });
          return HttpResponse.json({
            dry_run:
              new URL(request.url).searchParams.get("dry_run") === "true",
            changes: [
              {
                section: "platform",
                field: "banner",
                before: null,
                after: { text: "Hi" },
              },
            ],
            etag: '"all-ffff"',
          });
        }),
        ...stack(),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Import" }, LOAD),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Import settings",
    });
    const file = new File(['{"format":"contractum-settings"}'], "s.json", {
      type: "application/json",
    });
    await actor.upload(
      within(dialog).getByLabelText("Settings file (.json)"),
      file,
    );
    expect(await within(dialog).findByRole("table")).toBeVisible();
    expect(within(dialog).getByText("banner")).toBeVisible();
    await actor.click(within(dialog).getByRole("button", { name: "Apply" }));
    await waitFor(() => {
      expect(calls).toEqual([
        { dry: "true", etag: null },
        { dry: "false", etag: '"all-ffff"' },
      ]);
    });
  });

  it("says 'No changes' and keeps Apply disabled for an unchanged file", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/settings", {
      me: admin,
      folders,
      handlers: [
        http.post(`${SETTINGS}/import`, () =>
          HttpResponse.json({ dry_run: true, changes: [], etag: '"all-1"' }),
        ),
        ...stack(),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Import" }, LOAD),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Import settings",
    });
    await actor.upload(
      within(dialog).getByLabelText("Settings file (.json)"),
      new File(["{}"], "s.json", { type: "application/json" }),
    );
    expect(await within(dialog).findByText("No changes")).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Apply" }),
    ).toHaveAttribute("disabled");
  });

  it("shows a 422 with its pointer and refuses a file over 256 KiB", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/settings", {
      me: admin,
      folders,
      handlers: [
        http.post(`${SETTINGS}/import`, () =>
          problem(422, "settings-import-invalid", {
            errors: [
              { pointer: "/sections/nope", detail: "unknown settings section" },
            ],
          }),
        ),
        ...stack(),
      ],
    });
    await actor.click(
      await screen.findByRole("button", { name: "Import" }, LOAD),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Import settings",
    });
    const input = within(dialog).getByLabelText("Settings file (.json)");
    await actor.upload(
      input,
      new File(["{}"], "s.json", { type: "application/json" }),
    );
    expect(
      (await within(dialog).findAllByText(/unknown settings section/)).length,
    ).toBeGreaterThan(0);
    await actor.upload(
      input,
      new File(["x".repeat(256 * 1024 + 1)], "big.json", {
        type: "application/json",
      }),
    );
    expect(
      await within(dialog).findByText("This file is larger than 256 KiB."),
    ).toBeVisible();
  });
});

describe("the announcement banner", () => {
  const publicBanner = (banner: unknown) =>
    http.get(`${SETTINGS}/public`, () =>
      HttpResponse.json({
        banner,
        entra_default_tenant_id: null,
        entra_default_owner_folder_id: null,
      }),
    );

  it("shows an active banner to a user without settings access, as plain text, and dismisses it", async () => {
    const actor = userEvent.setup();
    renderApp("/", {
      me: makeMe({ global_permissions: ["audit:list"] }),
      folders,
      handlers: [
        publicBanner({
          text: "Downtime <script>alert(1)</script> tonight",
          severity: "warning",
          starts_at: null,
          ends_at: null,
        }),
      ],
    });
    const text = await screen.findByText(
      "Downtime <script>alert(1)</script> tonight",
      {},
      LOAD,
    );
    expect(text).toBeVisible();
    expect(document.querySelector("main script")).toBeNull();
    await actor.click(screen.getByRole("button", { name: "Dismiss alert" }));
    expect(screen.queryByText(/Downtime/)).toBeNull();
    expect(
      Object.keys(window.sessionStorage).some((k) =>
        k.startsWith("dc-banner-dismissed:"),
      ),
    ).toBe(true);
  });

  it("shows nothing when there is no banner or the request fails", async () => {
    renderApp("/", {
      me: admin,
      folders,
      handlers: [
        http.get(`${SETTINGS}/public`, () =>
          problem(503, "service-unavailable"),
        ),
      ],
    });
    await screen.findByRole("heading", { level: 1 }, LOAD);
    expect(screen.queryByText("Announcement")).toBeNull();
  });
});

describe("the public settings read", () => {
  it("shows no banner when the answer has no body", async () => {
    renderApp("/", {
      me: admin,
      folders,
      handlers: [http.get(`${SETTINGS}/public`, noBody)],
    });
    expect(
      await screen.findByRole("heading", { level: 1 }, LOAD),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Dismiss alert" })).toBeNull();
  });
});

describe("the Register dialogs pre-fill", () => {
  it("fills the default tenant and the default owner when it is an owner option", async () => {
    const actor = userEvent.setup();
    const entraAdmin = makeMe({
      global_permissions: [
        "user:read",
        "user:create",
        "entra:read",
        "entra:manage",
        "audit:list",
        "group:list",
      ],
    });
    renderApp("/admin/groups?source=entra", {
      me: entraAdmin,
      folders,
      config: { entraEnabled: true },
      handlers: [
        http.get(`${SETTINGS}/public`, () =>
          HttpResponse.json({
            banner: null,
            entra_default_tenant_id: OTHER,
            entra_default_owner_folder_id: "acme-id",
          }),
        ),
      ],
    });
    await actor.click(await screen.findByRole("button", { name: "New" }, LOAD));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Register Entra group" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Register Entra group",
    });
    await waitFor(() => {
      expect(within(dialog).getByLabelText("Tenant ID (optional)")).toHaveValue(
        OTHER,
      );
    });
    expect(within(dialog).getByLabelText("Owner")).toHaveValue("acme-id");
  });

  it("ignores a default owner the caller cannot use", async () => {
    const actor = userEvent.setup();
    renderApp("/admin/groups?source=entra", {
      me: makeMe({
        global_permissions: [
          "entra:read",
          "entra:manage",
          "audit:list",
          "group:list",
        ],
      }),
      folders,
      config: { entraEnabled: true },
      handlers: [
        http.get(`${SETTINGS}/public`, () =>
          HttpResponse.json({
            banner: null,
            entra_default_tenant_id: null,
            entra_default_owner_folder_id: "unknown-folder",
          }),
        ),
      ],
    });
    await actor.click(await screen.findByRole("button", { name: "New" }, LOAD));
    await actor.click(
      await screen.findByRole("menuitem", { name: "Register Entra group" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Register Entra group",
    });
    expect(within(dialog).getByLabelText("Owner")).toHaveValue(ROOT_ID);
  });
});
