// Admin > Settings, the states around the forms (quick 261121-bvy): a failed or empty settings read,
// switching tabs, a refused save, Export, the import dialog's cancel, close and refusal paths, the
// Entra tab's stale tenant, owner and overview variants, the banner window and About. The API is
// stubbed with msw.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { renderApp } from "@/test/renderApp";
import {
  aboutBody,
  admin,
  folders,
  LOAD,
  noBody,
  OTHER,
  overviewBody,
  problem,
  section,
  settingsBody,
  settingsSequence,
  SETTINGS,
  stack,
} from "@/test/settings-fixtures";

afterEach(() => {
  tokenStore.clear();
  vi.restoreAllMocks();
});

/** The settings page at `path` with `extra` handlers ahead of the default stack. */
function show(
  path: string,
  extra: RequestHandler[] = [],
  stored: Parameters<typeof stack> = [],
) {
  const { router } = renderApp(path, {
    me: admin,
    folders,
    handlers: [...extra, ...stack(...stored)],
  });
  return { actor: userEvent.setup(), router };
}

const savePut = (answer: () => Response | Promise<Response>) =>
  http.put(`${SETTINGS}/:section`, answer);

async function saveEntra(actor: ReturnType<typeof userEvent.setup>) {
  await screen.findByLabelText("Default tenant", {}, LOAD);
  await actor.click(
    screen.getByRole("button", { name: "Save Entra defaults" }),
  );
}

describe("the Settings page states", () => {
  it("shows a failed settings read with Retry loading, then the tabs", async () => {
    let fail = true;
    const { actor } = show("/admin/settings", [
      http.get(SETTINGS, () =>
        fail
          ? problem(503, "service-unavailable")
          : HttpResponse.json(settingsBody()),
      ),
    ]);
    const retry = await screen.findByRole(
      "button",
      { name: "Retry loading" },
      LOAD,
    );
    expect(screen.queryByRole("tab")).toBeNull();
    fail = false;
    await actor.click(retry);
    expect(
      await screen.findByRole("tab", { name: "Entra" }, LOAD),
    ).toBeVisible();
  });

  it("refuses a settings answer without a body with the generic alert", async () => {
    show("/admin/settings", [http.get(SETTINGS, noBody)]);
    expect(
      await screen.findByText("Something went wrong", {}, LOAD),
    ).toBeVisible();
  });

  it("says Not available when the API returns no section this UI knows", async () => {
    show("/admin/settings", [
      http.get(SETTINGS, () =>
        HttpResponse.json({ ...settingsBody(), sections: [] }),
      ),
    ]);
    expect(
      await screen.findByText("Not available at your access level", {}, LOAD),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Export" })).toBeVisible();
  });

  it("switches tabs and keeps the chosen tab in the URL", async () => {
    const { actor, router } = show("/admin/settings");
    await actor.click(
      await screen.findByRole("tab", { name: "Platform" }, LOAD),
    );
    expect(await screen.findByLabelText("Banner text")).toBeVisible();
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ tab: "platform" });
    });
  });

  it("reloads the settings from 'changed elsewhere'", async () => {
    const list = settingsSequence([settingsBody()]);
    const { actor } = show("/admin/settings?tab=entra", [
      list.handler,
      savePut(() => problem(412, "precondition-failed")),
    ]);
    await saveEntra(actor);
    const reload = await screen.findByRole("button", {
      name: "Reload settings",
    });
    const before = list.reads.count;
    await actor.click(reload);
    await waitFor(() => {
      expect(list.reads.count).toBeGreaterThan(before);
    });
  });

  it("shows any other save refusal in a problem alert", async () => {
    const { actor } = show("/admin/settings?tab=entra", [
      savePut(() => problem(500, "internal")),
    ]);
    await saveEntra(actor);
    expect(await screen.findByText("Refused")).toBeVisible();
    expect(screen.queryByText("These settings changed elsewhere")).toBeNull();
  });

  it("refuses a save answer without a body", async () => {
    const { actor } = show("/admin/settings?tab=entra", [savePut(noBody)]);
    await saveEntra(actor);
    expect(await screen.findByText("Something went wrong")).toBeVisible();
    expect(screen.queryByText("Entra defaults saved.")).toBeNull();
  });
});

describe("Export", () => {
  function captureLinks() {
    const links: { download: string; href: string }[] = [];
    const created = vi.fn((blob: Blob) => `blob:settings-${String(blob.size)}`);
    const revoked = vi.fn();
    Object.assign(URL, { createObjectURL: created, revokeObjectURL: revoked });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      function record(this: HTMLAnchorElement) {
        links.push({ download: this.download, href: this.href });
      },
    );
    return { links, revoked };
  }

  const exported = (headers: Record<string, string>) =>
    http.get(
      `${SETTINGS}/export`,
      () => new HttpResponse('{"format":"contractum-settings"}', { headers }),
    );

  it("downloads the file under the server's name and releases the URL", async () => {
    const { links, revoked } = captureLinks();
    const { actor } = show("/admin/settings", [
      exported({
        "Content-Disposition":
          'attachment; filename="settings-2026-11-21.json"',
      }),
    ]);
    await actor.click(
      await screen.findByRole("button", { name: "Export" }, LOAD),
    );
    await waitFor(() => {
      expect(links).toHaveLength(1);
    });
    expect(links[0]?.download).toBe("settings-2026-11-21.json");
    expect(revoked).toHaveBeenCalledWith(links[0]?.href);
    expect(document.querySelector("a[download]")).toBeNull();
  });

  it("falls back to a fixed name without a usable Content-Disposition", async () => {
    const { links } = captureLinks();
    const { actor } = show("/admin/settings", [
      exported({ "Content-Disposition": 'attachment; filename="../etc/x y"' }),
    ]);
    await actor.click(
      await screen.findByRole("button", { name: "Export" }, LOAD),
    );
    await waitFor(() => {
      expect(links.map((link) => link.download)).toEqual([
        "contractum-settings.json",
      ]);
    });
  });

  it("falls back to the fixed name when the answer has no Content-Disposition", async () => {
    const { links } = captureLinks();
    const { actor } = show("/admin/settings", [exported({})]);
    await actor.click(
      await screen.findByRole("button", { name: "Export" }, LOAD),
    );
    await waitFor(() => {
      expect(links.map((link) => link.download)).toEqual([
        "contractum-settings.json",
      ]);
    });
  });

  it("shows a refused export as an action problem", async () => {
    const { actor } = show("/admin/settings", [
      http.get(`${SETTINGS}/export`, () => problem(403, "forbidden")),
    ]);
    await actor.click(
      await screen.findByRole("button", { name: "Export" }, LOAD),
    );
    expect(await screen.findByRole("alert")).toBeVisible();
  });
});

describe("the import dialog", () => {
  const changes = [
    {
      section: "platform",
      field: "banner",
      before: null,
      after: { text: "Hi" },
    },
  ];

  async function openImport(extra: RequestHandler[]) {
    const { actor } = show("/admin/settings", extra);
    await actor.click(
      await screen.findByRole("button", { name: "Import" }, LOAD),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Import settings",
    });
    const input = within(dialog).getByLabelText("Settings file (.json)");
    const file = new File(["{}"], "s.json", { type: "application/json" });
    return { actor, dialog, input, file };
  }

  const dryRunThen = (onApply: () => Response) =>
    http.post(`${SETTINGS}/import`, ({ request }) =>
      new URL(request.url).searchParams.get("dry_run") === "true"
        ? HttpResponse.json({ dry_run: true, changes, etag: '"all-1"' })
        : onApply(),
    );

  it("closes on Cancel and opens again empty", async () => {
    const { actor, dialog, input, file } = await openImport([
      dryRunThen(() => problem(500, "internal")),
    ]);
    await actor.upload(input, file);
    expect(await within(dialog).findByRole("table")).toBeVisible();
    await actor.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await actor.click(screen.getByRole("button", { name: "Import" }));
    const again = await screen.findByRole("dialog", {
      name: "Import settings",
    });
    expect(within(again).queryByRole("table")).toBeNull();
  });

  it("closes from its own close button", async () => {
    const { actor, dialog } = await openImport([]);
    await actor.click(
      within(dialog).getByRole("button", { name: "Close dialog" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("says the settings changed elsewhere when Apply meets a 412", async () => {
    const { actor, dialog, input, file } = await openImport([
      dryRunThen(() => problem(412, "precondition-failed")),
    ]);
    await actor.upload(input, file);
    await actor.click(
      await within(dialog).findByRole("button", { name: "Apply" }),
    );
    expect(
      await within(dialog).findByText(
        /Settings changed after you chose the file/,
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("dialog", { name: "Import settings" }),
    ).toBeVisible();
  });

  it("shows any other Apply refusal and stays open", async () => {
    const { actor, dialog, input, file } = await openImport([
      dryRunThen(() => problem(500, "internal")),
    ]);
    await actor.upload(input, file);
    await actor.click(
      await within(dialog).findByRole("button", { name: "Apply" }),
    );
    expect(await within(dialog).findByText("Refused")).toBeVisible();
  });

  it("toasts and closes after a successful Apply", async () => {
    const { actor, dialog, input, file } = await openImport([
      dryRunThen(() =>
        HttpResponse.json({ dry_run: false, changes, etag: '"all-2"' }),
      ),
    ]);
    await actor.upload(input, file);
    await actor.click(
      await within(dialog).findByRole("button", { name: "Apply" }),
    );
    expect(await screen.findByText("Settings imported.")).toBeVisible();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("refuses a dry run without a body", async () => {
    const { actor, dialog, input, file } = await openImport([
      http.post(`${SETTINGS}/import`, noBody),
    ]);
    await actor.upload(input, file);
    expect(
      await within(dialog).findByText("Something went wrong"),
    ).toBeVisible();
  });

  it("clears the result when the file choice is emptied", async () => {
    const { actor, dialog, input, file } = await openImport([
      dryRunThen(() => problem(500, "internal")),
    ]);
    await actor.upload(input, file);
    expect(await within(dialog).findByRole("table")).toBeVisible();
    await actor.upload(input, []);
    await waitFor(() => {
      expect(within(dialog).queryByRole("table")).toBeNull();
    });
    expect(
      within(dialog).getByRole("button", { name: "Apply" }),
    ).toBeDisabled();
  });
});

describe("the Entra defaults form", () => {
  it("flags a saved tenant that is no longer configured", async () => {
    show(
      "/admin/settings?tab=entra",
      [],
      [{ default_tenant_id: "gone-tenant" }],
    );
    expect(
      await screen.findByText("Saved tenant is not configured", {}, LOAD),
    ).toBeVisible();
    const option = screen.getByRole("option", {
      name: "gone-tenant (no longer configured)",
    });
    expect(option).toBeDisabled();
  });

  it("saves a chosen owner, takes the stored values after each save and resets the owner", async () => {
    const bodies = [
      settingsBody(),
      {
        ...settingsBody(),
        sections: [
          section(
            "entra",
            { default_tenant_id: OTHER, default_owner_folder_id: "acme-id" },
            2,
          ),
          section("platform", {}, 2),
        ],
      },
      {
        ...settingsBody(),
        sections: [section("entra", {}, 3), section("platform", {}, 2)],
      },
    ];
    const list = settingsSequence(bodies);
    const puts: unknown[] = [];
    const { actor } = show("/admin/settings?tab=entra", [
      list.handler,
      http.put(`${SETTINGS}/:section`, async ({ request }) => {
        puts.push(await request.json());
        return HttpResponse.json(section("entra", {}, puts.length + 1));
      }),
    ]);
    const owner = await screen.findByLabelText(
      "Default owner for registered Entra identities",
      {},
      LOAD,
    );
    await actor.selectOptions(owner, "acme-id");
    await actor.selectOptions(screen.getByLabelText("Default tenant"), OTHER);
    await actor.click(
      screen.getByRole("button", { name: "Save Entra defaults" }),
    );
    expect(await screen.findByText("Entra defaults saved.")).toBeVisible();
    expect(puts[0]).toEqual({
      default_tenant_id: OTHER,
      default_owner_folder_id: "acme-id",
    });
    // The refetched section (revision 2) holds both values; the form shows them.
    await waitFor(() => {
      expect(screen.getByLabelText("Default tenant")).toHaveValue(OTHER);
    });
    expect(
      screen.getByLabelText("Default owner for registered Entra identities"),
    ).toHaveValue("acme-id");
    await actor.click(
      screen.getByRole("button", {
        name: "Reset to default for default owner",
      }),
    );
    await waitFor(() => {
      expect(puts[1]).toEqual({ default_tenant_id: OTHER });
    });
    // Revision 3 stores nothing: both fields go back to their placeholders.
    await waitFor(() => {
      expect(screen.getByLabelText("Default tenant")).toHaveValue("");
    });
    expect(
      screen.getByLabelText("Default owner for registered Entra identities"),
    ).toHaveValue("");
  });

  it("puts a 422 on the owner under its field", async () => {
    const { actor } = show("/admin/settings?tab=entra", [
      savePut(() =>
        problem(422, "validation-error", {
          errors: [
            {
              pointer: "/default_owner_folder_id",
              detail: "Choose ROOT or an Organization.",
            },
          ],
        }),
      ),
    ]);
    await saveEntra(actor);
    expect(
      await screen.findByText("Choose ROOT or an Organization."),
    ).toBeVisible();
    expect(screen.queryByText("Refused")).toBeNull();
  });
});

describe("the Entra overview", () => {
  const overview = (changes: Record<string, unknown>) =>
    http.get(`${SETTINGS}/entra/overview`, () =>
      HttpResponse.json({ ...overviewBody, ...changes }),
    );

  it("words an unconfigured deployment without a reminder", async () => {
    show("/admin/settings?tab=entra", [
      overview({
        tenants: [],
        graph_client_id: null,
        graph_secret_configured: false,
        signin_configured: false,
        secret_expiry: {
          status: "not-configured",
          expires_at: null,
          days_left: null,
          reason: null,
        },
      }),
    ]);
    expect(
      await screen.findByText("No Graph client configured", {}, LOAD),
    ).toBeVisible();
    expect(screen.getByText("Not configured")).toBeVisible();
    expect(screen.getByText("None")).toBeVisible();
    expect(screen.getByText("Not set")).toBeVisible();
    expect(screen.getByText("No secret file")).toBeVisible();
    expect(screen.queryByText("Rotate the Graph client secret")).toBeNull();
  });

  it("reminds to rotate a known expiry without a day count", async () => {
    show("/admin/settings?tab=entra", [
      overview({
        secret_expiry: {
          status: "known",
          expires_at: "2026-12-01T00:00:00Z",
          days_left: null,
          reason: null,
        },
      }),
    ]);
    expect(await screen.findByText(/\(0 days left\)/, {}, LOAD)).toBeVisible();
    expect(screen.getByText("Rotate the Graph client secret")).toBeVisible();
  });

  it("shows a failed overview with Retry loading", async () => {
    let fail = true;
    const { actor } = show("/admin/settings?tab=entra", [
      http.get(`${SETTINGS}/entra/overview`, () =>
        fail ? problem(500, "internal") : HttpResponse.json(overviewBody),
      ),
    ]);
    const retry = await screen.findByRole(
      "button",
      { name: "Retry loading" },
      LOAD,
    );
    fail = false;
    await actor.click(retry);
    expect(
      await screen.findByText("Managed in Helm (view only)", {}, LOAD),
    ).toBeVisible();
  });

  it("refuses an overview answer without a body", async () => {
    show("/admin/settings?tab=entra", [
      http.get(`${SETTINGS}/entra/overview`, noBody),
    ]);
    expect(
      await screen.findByText("Something went wrong", {}, LOAD),
    ).toBeVisible();
  });

  it("says the test could not run on another failure", async () => {
    const { actor } = show("/admin/settings?tab=entra", [
      http.post(`${SETTINGS}/entra/test`, noBody),
    ]);
    await actor.click(
      await screen.findByRole("button", { name: "Test connection" }, LOAD),
    );
    expect(await screen.findByText("The test could not run")).toBeVisible();
  });
});

describe("the banner form", () => {
  const stored = {
    banner: {
      text: "Planned upgrade",
      severity: "warning",
      starts_at: "2026-12-01T08:00:00Z",
      ends_at: "2026-12-01T10:00:00Z",
    },
  };

  it("loads a stored banner with its window and clears it with Reset to default", async () => {
    const puts: unknown[] = [];
    const { actor } = show(
      "/admin/settings?tab=platform",
      [
        http.put(`${SETTINGS}/:section`, async ({ request }) => {
          puts.push(await request.json());
          return HttpResponse.json(section("platform", {}, 3));
        }),
      ],
      [{}, stored],
    );
    expect(await screen.findByLabelText("Banner text", {}, LOAD)).toHaveValue(
      "Planned upgrade",
    );
    expect(screen.getByRole("button", { name: "Warning" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByLabelText(/^Starts/)).not.toHaveValue("");
    await actor.click(screen.getByRole("button", { name: "Reset to default" }));
    await waitFor(() => {
      expect(puts).toEqual([{}]);
    });
    expect(await screen.findByText("Banner cleared.")).toBeVisible();
    expect(screen.getByLabelText("Banner text")).toHaveValue("");
  });

  it("refuses an end before the start, then sends the window in UTC", async () => {
    const puts: unknown[] = [];
    const { actor } = show("/admin/settings?tab=platform", [
      http.put(`${SETTINGS}/:section`, async ({ request }) => {
        puts.push(await request.json());
        return HttpResponse.json(section("platform", {}, 3));
      }),
    ]);
    await actor.type(
      await screen.findByLabelText("Banner text", {}, LOAD),
      "Window",
    );
    const starts = screen.getByLabelText(/^Starts/);
    const ends = screen.getByLabelText(/^Ends/);
    await actor.type(starts, "2026-12-01T10:00");
    await actor.type(ends, "2026-12-01T09:00");
    await actor.click(screen.getByRole("button", { name: "Save banner" }));
    expect(
      await screen.findByText("The end must be after the start."),
    ).toBeVisible();
    expect(puts).toEqual([]);
    await actor.clear(ends);
    await actor.type(ends, "2026-12-01T11:00");
    await actor.click(screen.getByRole("button", { name: "Save banner" }));
    await waitFor(() => {
      expect(puts).toHaveLength(1);
    });
    expect(puts[0]).toEqual({
      banner: {
        text: "Window",
        severity: "info",
        starts_at: new Date("2026-12-01T10:00").toISOString(),
        ends_at: new Date("2026-12-01T11:00").toISOString(),
      },
    });
  });

  it("puts 422 pointers on the text and the window", async () => {
    const { actor } = show("/admin/settings?tab=platform", [
      savePut(() =>
        problem(422, "validation-error", {
          errors: [
            { pointer: "/banner/text", detail: "Too long for a banner." },
            { pointer: "/banner", detail: "The window is in the past." },
          ],
        }),
      ),
    ]);
    await actor.type(
      await screen.findByLabelText("Banner text", {}, LOAD),
      "Hello",
    );
    await actor.click(screen.getByRole("button", { name: "Save banner" }));
    expect(await screen.findByText("Too long for a banner.")).toBeVisible();
    expect(screen.getByText("The window is in the past.")).toBeVisible();
    expect(screen.queryByText("Refused")).toBeNull();
  });

  it("takes the new stored banner after a save, and an emptied one after the next", async () => {
    const platform = (values: Record<string, unknown>, revision: number) => ({
      ...settingsBody(),
      sections: [
        section("entra", {}, 1),
        section("platform", values, revision),
      ],
    });
    const list = settingsSequence([
      platform({}, 2),
      platform({ banner: { text: "Stored text", severity: "info" } }, 3),
      platform({ banner: { severity: "info" } }, 4),
    ]);
    const { actor } = show("/admin/settings?tab=platform", [
      list.handler,
      savePut(() => HttpResponse.json(section("platform", {}, 9))),
    ]);
    const text = await screen.findByLabelText("Banner text", {}, LOAD);
    await actor.type(text, "Typed text");
    await actor.click(screen.getByRole("button", { name: "Save banner" }));
    await waitFor(() => {
      expect(screen.getByLabelText("Banner text")).toHaveValue("Stored text");
    });
    expect(screen.getByText("Banner saved.")).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Save banner" }));
    // A stored banner without text is not a banner: the form empties.
    await waitFor(() => {
      expect(screen.getByLabelText("Banner text")).toHaveValue("");
    });
  });
});

describe("About", () => {
  it("formats a real build time and keeps a missing one as a dash", async () => {
    show("/admin/settings?tab=platform", [
      http.get(`${SETTINGS}/about`, () =>
        HttpResponse.json({
          services: [
            { ...aboutBody.services[0], built_at: "2026-11-20T09:30:00Z" },
            aboutBody.services[4],
          ],
        }),
      ),
    ]);
    const table = await screen.findByRole("table", { name: "Services" }, LOAD);
    const identity = within(table).getByRole("row", { name: /identity/ });
    expect(within(identity).queryByText("2026-11-20T09:30:00Z")).toBeNull();
    expect(within(identity).getByText(/2026/)).toBeVisible();
    const mail = within(table).getByRole("row", { name: /mail/ });
    expect(within(mail).getAllByText("—").length).toBeGreaterThan(0);
  });

  it("shows a failed About with Retry loading", async () => {
    let fail = true;
    const { actor } = show("/admin/settings?tab=platform", [
      http.get(`${SETTINGS}/about`, () =>
        fail ? problem(500, "internal") : HttpResponse.json(aboutBody),
      ),
    ]);
    const retry = await screen.findByRole(
      "button",
      { name: "Retry loading" },
      LOAD,
    );
    fail = false;
    await actor.click(retry);
    expect(
      await screen.findByRole("table", { name: "Services" }, LOAD),
    ).toBeVisible();
  });

  it("refuses an About answer without a body", async () => {
    show("/admin/settings?tab=platform", [
      http.get(`${SETTINGS}/about`, noBody),
    ]);
    expect(
      await screen.findByText("Something went wrong", {}, LOAD),
    ).toBeVisible();
  });
});
