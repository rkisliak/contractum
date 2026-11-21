// Settings > Notifications (quick 261120-0yd): the tab is built from the section the API returns,
// templates with preview and reset, the sender and the test-email dialog, retries, sunset leads and
// the webhook host list. The API is stubbed with msw.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { SettingsOut } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, makeMe, renderApp, rootFolder } from "@/test/renderApp";
import { problem, settingsSequence } from "@/test/settings-fixtures";
import { humanSeconds } from "./NotificationsTab";

afterEach(() => {
  tokenStore.clear();
  window.sessionStorage.clear();
});

const LOAD = { timeout: 10_000 };
const admin = makeMe({
  global_permissions: ["settings:read", "settings:write", "audit:list"],
});
const folders = [rootFolder, makeFolder({ id: "acme-id", slug: "acme" })];
const SETTINGS = "*/api/v1/identity/settings";
const PREVIEW = "*/api/v1/identity/settings/notifications/preview";
const TEST_EMAIL = "*/api/v1/contracts/settings/test-email";
const PUBLISHED = "contract.version.published";
const DEPRECATED = "contract.version.deprecated";

const EVENTS = [
  PUBLISHED,
  "contract.version.breaking_published",
  "contract.latest.changed",
  DEPRECATED,
  "contract.version.sunset_approaching",
  "contract.version.unpublish_blocked",
  "card.subscription.removed",
  "generic",
];
const DEFAULT_SUBJECT = (event: string) => `[Contractum] {odcs} ${event}`;
const DEFAULT_BODY = "{subject}\n\n{details}{breaking_changes}\n";

function notifications(values: Record<string, unknown> = {}, revision = 3) {
  return {
    section: "notifications",
    values,
    defaults: {
      email_templates: Object.fromEntries(
        EVENTS.map((e) => [
          e,
          { subject: DEFAULT_SUBJECT(e), body: DEFAULT_BODY },
        ]),
      ),
      retry_schedule_s: [60, 300, 1800],
      max_attempts: 7,
      sunset_lead_days: [7, 30],
      sender: { name: null, address: "dc@example.test" },
      webhook_allowed_hosts: [],
    },
    defaults_source: "service" as const,
    revision,
    etag: `"notifications-r${String(revision)}"`,
    updated_at: null,
    updated_by: null,
  };
}

function body(values: Record<string, unknown> = {}): SettingsOut {
  return {
    sections: [
      {
        section: "entra",
        values: {},
        defaults: {},
        defaults_source: "identity",
        revision: 1,
        etag: '"entra-r1"',
        updated_at: null,
        updated_by: null,
      },
      notifications(values),
    ],
    etag: '"all-0123456789abcdef"',
    choices: {
      entra_tenants: [],
      odcs_api_versions: ["v3.1.0", "v3.2.0"],
      microsoft_signin_available: true,
    },
  };
}

function open(
  values: Record<string, unknown> = {},
  handlers: NonNullable<
    NonNullable<Parameters<typeof renderApp>[1]>["handlers"]
  > = [],
) {
  return renderApp("/admin/settings?tab=notifications", {
    me: admin,
    folders,
    handlers: [
      ...handlers,
      http.get(SETTINGS, () => HttpResponse.json(body(values))),
    ],
  });
}

describe("the Notifications tab", () => {
  it("appears after Entra when the API returns the section, with plain event labels", async () => {
    open();
    const tab = await screen.findByRole(
      "tab",
      { name: "Notifications", selected: true },
      LOAD,
    );
    expect(tab).toBeVisible();
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(tabs.indexOf("Entra")).toBeLessThan(tabs.indexOf("Notifications"));
    const select = screen.getByLabelText("Notice");
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual([
      "Version published",
      "Published with breaking changes",
      "Latest version changed",
      "Version deprecated",
      "Sunset approaching",
      "Unpublish blocked",
      "Pin removed",
      "Other notices",
    ]);
    expect(screen.getByLabelText("Subject")).toHaveValue(
      DEFAULT_SUBJECT(PUBLISHED),
    );
    expect(screen.getByLabelText("Body")).toHaveValue(DEFAULT_BODY);
    expect(
      screen.getAllByLabelText("Placeholders you can use")[0],
    ).toHaveTextContent("{card}");
  });

  it("saves the whole section with If-Match and stores only what differs from the defaults", async () => {
    const actor = userEvent.setup();
    let seen: { etag: string | null; body: unknown } | null = null;
    open({}, [
      http.put(`${SETTINGS}/:section`, async ({ request }) => {
        seen = {
          etag: request.headers.get("if-match"),
          body: await request.json(),
        };
        return HttpResponse.json(notifications({}, 4));
      }),
    ]);
    const subject = await screen.findByLabelText("Subject", {}, LOAD);
    await actor.clear(subject);
    await actor.click(subject);
    await actor.paste("{card}: {odcs} is out");
    await actor.click(
      screen.getByRole("button", { name: "Save notification settings" }),
    );
    await waitFor(() => {
      expect(seen).toEqual({
        etag: '"notifications-r3"',
        body: {
          email_templates: {
            [PUBLISHED]: { subject: "{card}: {odcs} is out" },
          },
        },
      });
    });
    expect(
      await screen.findByText("Notification settings saved."),
    ).toBeVisible();
  });

  it("saves an unchanged form as an empty section", async () => {
    const actor = userEvent.setup();
    let sent: unknown = null;
    open({}, [
      http.put(`${SETTINGS}/:section`, async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json(notifications({}, 3));
      }),
    ]);
    await screen.findByLabelText("Subject", {}, LOAD);
    await actor.click(
      screen.getByRole("button", { name: "Save notification settings" }),
    );
    await waitFor(() => {
      expect(sent).toEqual({});
    });
  });

  it("previews the rendered subject and body from the preview route", async () => {
    const actor = userEvent.setup();
    let seen: unknown = null;
    open({}, [
      http.post(PREVIEW, async ({ request }) => {
        seen = await request.json();
        return HttpResponse.json({
          subject: "Analytics team: orders is out",
          body: "Line one\nLine two",
        });
      }),
    ]);
    const subject = await screen.findByLabelText("Subject", {}, LOAD);
    await actor.clear(subject);
    await actor.click(subject);
    await actor.paste("{card}: {odcs} is out");
    await actor.click(screen.getByRole("button", { name: "Preview" }));
    const box = await screen.findByTestId("template-preview");
    expect(box).toHaveTextContent("Analytics team: orders is out");
    expect(box).toHaveTextContent("Line one");
    expect(seen).toEqual({
      event_type: PUBLISHED,
      subject: "{card}: {odcs} is out",
      body: DEFAULT_BODY,
    });
  });

  it("puts a 422 from the preview on the subject field", async () => {
    const actor = userEvent.setup();
    open({}, [
      http.post(PREVIEW, () =>
        problem(422, "validation-error", {
          errors: [
            { pointer: "/subject", detail: "Unknown placeholder 'bogus'." },
          ],
        }),
      ),
    ]);
    await screen.findByLabelText("Subject", {}, LOAD);
    await actor.click(screen.getByRole("button", { name: "Preview" }));
    expect(
      await screen.findByText("Unknown placeholder 'bogus'."),
    ).toBeVisible();
  });

  it("resets a stored template to default by saving the section without it", async () => {
    const actor = userEvent.setup();
    let sent: unknown = null;
    open(
      {
        email_templates: {
          [PUBLISHED]: { subject: "mine" },
          [DEPRECATED]: { subject: "other" },
        },
        max_attempts: 3,
      },
      [
        http.put(`${SETTINGS}/:section`, async ({ request }) => {
          sent = await request.json();
          return HttpResponse.json(notifications({}, 4));
        }),
      ],
    );
    expect(await screen.findByLabelText("Subject", {}, LOAD)).toHaveValue(
      "mine",
    );
    await actor.click(
      screen.getByRole("button", {
        name: "Reset to default for this template",
      }),
    );
    await waitFor(() => {
      expect(sent).toEqual({
        email_templates: { [DEPRECATED]: { subject: "other" } },
        max_attempts: 3,
      });
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Subject")).toHaveValue(
        DEFAULT_SUBJECT(PUBLISHED),
      );
    });
  });

  it("puts a save 422 on the template field and selects the event it belongs to", async () => {
    const actor = userEvent.setup();
    open({}, [
      http.put(`${SETTINGS}/:section`, () =>
        problem(422, "validation-error", {
          errors: [
            {
              pointer: `/email_templates/${DEPRECATED}/subject`,
              detail: "Unknown placeholder 'bogus'.",
            },
          ],
        }),
      ),
    ]);
    await screen.findByLabelText("Subject", {}, LOAD);
    await actor.click(
      screen.getByRole("button", { name: "Save notification settings" }),
    );
    expect(
      await screen.findByText("Unknown placeholder 'bogus'."),
    ).toBeVisible();
    expect(screen.getByLabelText("Notice")).toHaveValue(DEPRECATED);
  });

  it("shows 'changed elsewhere' with a reload on a 412", async () => {
    const actor = userEvent.setup();
    open({}, [
      http.put(`${SETTINGS}/:section`, () =>
        problem(412, "precondition-failed"),
      ),
    ]);
    await screen.findByLabelText("Subject", {}, LOAD);
    await actor.click(
      screen.getByRole("button", { name: "Save notification settings" }),
    );
    expect(
      await screen.findByText("These settings changed elsewhere"),
    ).toBeVisible();
  });

  it("shows the default sender address and saves a sender name", async () => {
    const actor = userEvent.setup();
    let sent: unknown = null;
    open({}, [
      http.put(`${SETTINGS}/:section`, async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json(
          notifications({ sender: { name: "Contractum" } }, 4),
        );
      }),
    ]);
    await screen.findByLabelText(/Sender name/, {}, LOAD);
    expect(screen.getAllByText(/Default:/)[0]).toHaveTextContent(
      "dc@example.test",
    );
    await actor.type(screen.getByLabelText(/Sender name/), "Contractum");
    await actor.click(
      screen.getByRole("button", { name: "Save notification settings" }),
    );
    await waitFor(() => {
      expect(sent).toEqual({ sender: { name: "Contractum" } });
    });
  });

  it("refuses an emoji in the sender name and sends nothing", async () => {
    const actor = userEvent.setup();
    let saved = false;
    open({}, [
      http.put(`${SETTINGS}/:section`, () => {
        saved = true;
        return HttpResponse.json(notifications({}, 4));
      }),
    ]);
    await screen.findByLabelText(/Sender name/, {}, LOAD);
    await actor.type(screen.getByLabelText(/Sender name/), "Contractum 😀");
    expect(
      screen.getByText(
        "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.",
      ),
    ).toBeVisible();
    await actor.click(
      screen.getByRole("button", { name: "Save notification settings" }),
    );
    expect(saved).toBe(false);
  });

  it.each([
    ["sent", "Test email sent", "info"],
    ["refused", "Test email refused", "warning"],
    ["unavailable", "Mail is unavailable", "warning"],
    ["not-configured", "Email is not configured", "warning"],
  ])("sends a test email and reads the %s outcome", async (outcome, title) => {
    const actor = userEvent.setup();
    let seen: unknown = null;
    open({}, [
      http.post(TEST_EMAIL, async ({ request }) => {
        seen = await request.json();
        return HttpResponse.json({ outcome, error_code: null });
      }),
    ]);
    await screen.findByLabelText("Subject", {}, LOAD);
    await actor.click(screen.getByRole("button", { name: "Send test email" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Send a test email",
    });
    await actor.type(
      within(dialog).getByLabelText("Send to"),
      "me@example.com",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Send test email" }),
    );
    expect(await within(dialog).findByText(title)).toBeVisible();
    expect(seen).toEqual({ to: "me@example.com" });
  });

  it("validates the address before sending and shows the wait on a 429", async () => {
    const actor = userEvent.setup();
    let calls = 0;
    open({}, [
      http.post(TEST_EMAIL, () => {
        calls += 1;
        return problem(429, "too-many-attempts", { retry_after_s: 240 });
      }),
    ]);
    await screen.findByLabelText("Subject", {}, LOAD);
    await actor.click(screen.getByRole("button", { name: "Send test email" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Send a test email",
    });
    await actor.click(
      within(dialog).getByRole("button", { name: "Send test email" }),
    );
    expect(
      await within(dialog).findByText(/Enter an email address/),
    ).toBeVisible();
    expect(calls).toBe(0);
    await actor.type(
      within(dialog).getByLabelText("Send to"),
      "me@example.com",
    );
    await actor.click(
      within(dialog).getByRole("button", { name: "Send test email" }),
    );
    expect(
      await within(dialog).findByText(/Try again in about 4 minutes/),
    ).toBeVisible();
  });

  it("echoes the delays in plain units and saves a changed schedule, attempts and leads", async () => {
    const actor = userEvent.setup();
    let sent: unknown = null;
    open({}, [
      http.put(`${SETTINGS}/:section`, async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json(notifications({}, 4));
      }),
    ]);
    const delays = await screen.findByLabelText(
      "Retry delays (seconds)",
      {},
      LOAD,
    );
    expect(screen.getAllByText(/1 min, 5 min, 30 min/).length).toBeGreaterThan(
      0,
    );
    await actor.clear(delays);
    await actor.type(delays, "30, 120");
    expect(screen.getByText(/30 s, 2 min/)).toBeVisible();
    const attempts = screen.getByLabelText("Maximum attempts");
    await actor.clear(attempts);
    await actor.type(attempts, "4");
    const leads = screen.getByLabelText(/Reminder lead times/);
    await actor.clear(leads);
    await actor.type(leads, "14, 3");
    await actor.click(
      screen.getByRole("button", { name: "Save notification settings" }),
    );
    await waitFor(() => {
      expect(sent).toEqual({
        retry_schedule_s: [30, 120],
        max_attempts: 4,
        sunset_lead_days: [14, 3],
      });
    });
  });

  it("refuses non-numeric delays on the field without a request", async () => {
    const actor = userEvent.setup();
    let puts = 0;
    open({}, [
      http.put(`${SETTINGS}/:section`, () => {
        puts += 1;
        return HttpResponse.json(notifications({}, 4));
      }),
    ]);
    const delays = await screen.findByLabelText(
      "Retry delays (seconds)",
      {},
      LOAD,
    );
    await actor.clear(delays);
    await actor.type(delays, "soon");
    await actor.click(
      screen.getByRole("button", { name: "Save notification settings" }),
    );
    expect(
      await screen.findByText(/Enter whole numbers of seconds/),
    ).toBeVisible();
    expect(puts).toBe(0);
  });

  it("shows HTTPS only as enforced with no control, and saves the allowed hosts", async () => {
    const actor = userEvent.setup();
    let sent: unknown = null;
    open({}, [
      http.put(`${SETTINGS}/:section`, async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json(notifications({}, 4));
      }),
    ]);
    const hosts = await screen.findByLabelText(
      "Allowed webhook hosts",
      {},
      LOAD,
    );
    expect(screen.getByText("Enforced")).toBeVisible();
    expect(screen.getByText("HTTPS only")).toBeVisible();
    expect(screen.queryByRole("switch", { name: /HTTPS/ })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: /HTTPS/ })).toBeNull();
    expect(
      screen.getByText(
        "Webhooks are sent only to these hosts, one per line (hooks.example.com or *.example.com).",
      ),
    ).toBeVisible();
    // With nothing listed, the card says what that means.
    expect(
      screen.getByText(
        "No webhook host is allowed, so card owners cannot add webhooks and none are sent.",
      ),
    ).toBeVisible();
    expect(screen.getByText("Default: no host is allowed")).toBeVisible();
    await actor.type(hosts, "hooks.example.com{Enter}*.example.org");
    await actor.click(
      screen.getByRole("button", { name: "Save notification settings" }),
    );
    await waitFor(() => {
      expect(sent).toEqual({
        webhook_allowed_hosts: ["hooks.example.com", "*.example.org"],
      });
    });
  });

  it("drops the empty-list warning once a host is listed", async () => {
    const actor = userEvent.setup();
    open({ webhook_allowed_hosts: ["hooks.example.com"] });
    const hosts = await screen.findByLabelText(
      "Allowed webhook hosts",
      {},
      LOAD,
    );
    expect(hosts).toHaveValue("hooks.example.com");
    expect(screen.queryByText(/card owners cannot add webhooks/)).toBeNull();
    await actor.clear(hosts);
    expect(
      await screen.findByText(/card owners cannot add webhooks/),
    ).toBeVisible();
  });

  it("puts a host 422 on the hosts field", async () => {
    const actor = userEvent.setup();
    open({}, [
      http.put(`${SETTINGS}/:section`, () =>
        problem(422, "validation-error", {
          errors: [
            {
              pointer: "/webhook_allowed_hosts",
              detail: "Enter a lowercase hostname such as hooks.example.com.",
            },
          ],
        }),
      ),
    ]);
    const hosts = await screen.findByLabelText(
      "Allowed webhook hosts",
      {},
      LOAD,
    );
    await actor.type(hosts, "10.0.0.1");
    await actor.click(
      screen.getByRole("button", { name: "Save notification settings" }),
    );
    expect(
      await screen.findByText(
        "Enter a lowercase hostname such as hooks.example.com.",
      ),
    ).toBeVisible();
  });

  it("makes no notification request for a caller without settings:read", async () => {
    const calls: string[] = [];
    renderApp("/admin/settings?tab=notifications", {
      me: makeMe({ global_permissions: ["audit:list"] }),
      folders,
      handlers: [
        http.post(PREVIEW, ({ request }) => {
          calls.push(request.url);
          return HttpResponse.json({});
        }),
        http.post(TEST_EMAIL, ({ request }) => {
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
    expect(screen.queryByRole("tab", { name: "Notifications" })).toBeNull();
    expect(calls).toEqual([]);
  });
});

describe("humanSeconds", () => {
  it.each([
    [30, "30 s"],
    [60, "1 min"],
    [90, "1 min 30 s"],
    [300, "5 min"],
    [1800, "30 min"],
    [3600, "1 h"],
    [7200, "2 h"],
    [43200, "12 h"],
    [86400, "1 day"],
    [172800, "2 days"],
  ])("%i seconds reads %s", (seconds, text) => {
    expect(humanSeconds(seconds)).toBe(text);
  });
});

describe("the Notifications tab, stored values and refusals", () => {
  const saveButton = () =>
    screen.getByRole("button", { name: "Save notification settings" });

  /** PUT bodies in order; each save answers the next revision. */
  function capturePuts() {
    const bodies: unknown[] = [];
    const handler = http.put(`${SETTINGS}/:section`, async ({ request }) => {
      bodies.push(await request.json());
      return HttpResponse.json(notifications({}, 10 + bodies.length));
    });
    return { bodies, handler };
  }

  const stored = {
    email_templates: { [PUBLISHED]: { subject: "S1", body: "B1" } },
    sender: { name: "Data team", address: "data@example.test" },
    retry_schedule_s: [10],
    max_attempts: 3,
    sunset_lead_days: [14],
    webhook_allowed_hosts: ["hooks.example.com"],
  };

  it("starts from the stored values and resets each card on its own", async () => {
    const actor = userEvent.setup();
    const puts = capturePuts();
    open(stored, [puts.handler]);
    expect(await screen.findByLabelText("Body", {}, LOAD)).toHaveValue("B1");
    expect(screen.getByLabelText(/Sender name/)).toHaveValue("Data team");
    expect(screen.getByLabelText(/Sender address/)).toHaveValue(
      "data@example.test",
    );
    expect(screen.getByText(/^1 host listed\./)).toBeVisible();
    const resets: [string, string][] = [
      ["Reset to default for sender", "Sender reset to default."],
      ["Reset to default for retries", "Retries reset to default."],
      ["Reset to default for sunset reminders", "Reminders reset to default."],
      ["Reset to default for allowed hosts", "Allowed hosts reset to default."],
      ["Reset to default for this template", "Template reset to default."],
    ];
    for (const [name, message] of resets) {
      await actor.click(screen.getByRole("button", { name }));
      expect(await screen.findByText(message)).toBeVisible();
    }
    expect(puts.bodies[0]).not.toHaveProperty("sender");
    expect(puts.bodies[1]).toEqual({
      email_templates: stored.email_templates,
      sender: stored.sender,
      sunset_lead_days: [14],
      webhook_allowed_hosts: ["hooks.example.com"],
    });
    expect(puts.bodies[2]).not.toHaveProperty("sunset_lead_days");
    expect(puts.bodies[3]).not.toHaveProperty("webhook_allowed_hosts");
    // The only stored template goes, so the section is saved without templates.
    expect(puts.bodies[4]).not.toHaveProperty("email_templates");
  });

  it("falls back to the built-in values when the service reports no defaults", async () => {
    const actor = userEvent.setup();
    const puts = capturePuts();
    const list = body();
    const section = list.sections.find((s) => s.section === "notifications");
    if (section !== undefined) section.defaults = {};
    open({}, [puts.handler, http.get(SETTINGS, () => HttpResponse.json(list))]);
    expect(await screen.findByLabelText("Subject", {}, LOAD)).toHaveValue("");
    expect(screen.getByLabelText("Maximum attempts")).toHaveValue("7");
    expect(screen.getByText("the mail server's address")).toBeVisible();
    expect(screen.getByText(/up to 7 attempts/)).toBeVisible();
    await actor.click(saveButton());
    expect(
      await screen.findByText(
        "Enter a subject, or reset the template to default.",
      ),
    ).toBeVisible();
    expect(
      screen.getByText("Enter a body, or reset the template to default."),
    ).toBeVisible();
    expect(puts.bodies).toEqual([]);
  });

  it("refuses bad attempts and leads, then saves an address-only sender and another notice's body", async () => {
    const actor = userEvent.setup();
    const puts = capturePuts();
    open({}, [puts.handler]);
    const attempts = await screen.findByLabelText("Maximum attempts", {}, LOAD);
    const leads = screen.getByLabelText(
      "Reminder lead times (days before sunset)",
    );
    await actor.clear(attempts);
    await actor.type(attempts, "many");
    await actor.clear(leads);
    await actor.type(leads, "soon");
    await actor.click(saveButton());
    expect(
      await screen.findByText("Enter a whole number of attempts."),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Enter whole numbers of days separated by commas, such as 30, 7.",
      ),
    ).toBeVisible();
    expect(puts.bodies).toEqual([]);

    await actor.clear(attempts);
    await actor.type(attempts, "7");
    await actor.clear(leads);
    await actor.type(leads, "30, 7");
    await actor.type(
      screen.getByLabelText(/Sender address/),
      "ops@example.test",
    );
    await actor.selectOptions(screen.getByLabelText("Notice"), DEPRECATED);
    const bodyField = screen.getByLabelText("Body");
    await actor.clear(bodyField);
    await actor.type(bodyField, "Deprecated.");
    await actor.click(saveButton());
    await waitFor(() => {
      expect(puts.bodies).toHaveLength(1);
    });
    expect(puts.bodies[0]).toEqual({
      email_templates: { [DEPRECATED]: { body: "Deprecated." } },
      sender: { address: "ops@example.test" },
    });
  });

  it("puts each save 422 pointer on its field and the rest in the alert", async () => {
    const actor = userEvent.setup();
    open({}, [
      http.put(`${SETTINGS}/:section`, () =>
        problem(422, "validation-error", {
          errors: [
            { pointer: "/somewhere_else", detail: "Not a field here." },
            { pointer: "/sender/name", detail: "Name refused." },
            { pointer: "/sender/address", detail: "Address refused." },
            { pointer: "/retry_schedule_s/1", detail: "Delay refused." },
            { pointer: "/max_attempts", detail: "Attempts refused." },
            { pointer: "/max_attempts", detail: "Attempts again." },
            { pointer: "/sunset_lead_days/0", detail: "Lead refused." },
          ],
        }),
      ),
    ]);
    await screen.findByLabelText("Subject", {}, LOAD);
    await actor.click(saveButton());
    const fields: [RegExp | string, string][] = [
      [/Sender name/, "Name refused."],
      [/Sender address/, "Address refused."],
      ["Retry delays (seconds)", "Delay refused."],
      ["Maximum attempts", "Attempts refused."],
      ["Reminder lead times (days before sunset)", "Lead refused."],
    ];
    expect(await screen.findByText("Not a field here.")).toBeVisible();
    for (const [label, detail] of fields) {
      expect(screen.getByLabelText(label)).toHaveAccessibleDescription(
        expect.stringContaining(detail),
      );
    }
  });

  it("warns when the preview cannot run and puts a body 422 on the body", async () => {
    const actor = userEvent.setup();
    let answer: () => Response = () => problem(500, "internal");
    open({}, [http.post(PREVIEW, () => answer())]);
    await screen.findByLabelText("Subject", {}, LOAD);
    await actor.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("The preview could not run")).toBeVisible();
    answer = () =>
      problem(422, "validation-error", {
        errors: [{ pointer: "/body", detail: "Unclosed brace in the body." }],
      });
    await actor.click(screen.getByRole("button", { name: "Preview" }));
    expect(
      await screen.findByText("Unclosed brace in the body."),
    ).toBeVisible();
    expect(screen.queryByText("The preview could not run")).toBeNull();
  });

  it("refuses a preview answer without a body", async () => {
    const actor = userEvent.setup();
    open({}, [
      http.post(PREVIEW, () => new HttpResponse(null, { status: 204 })),
    ]);
    await screen.findByLabelText("Subject", {}, LOAD);
    await actor.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("The preview could not run")).toBeVisible();
  });

  it("takes the stored values of a new revision and keeps the chosen notice", async () => {
    const actor = userEvent.setup();
    const next = body({ max_attempts: 9 });
    next.sections = next.sections.map((entry) =>
      entry.section === "notifications"
        ? notifications({ max_attempts: 9 }, 4)
        : entry,
    );
    const reads = settingsSequence([body(), next]);
    open({}, [
      reads.handler,
      http.put(`${SETTINGS}/:section`, () =>
        HttpResponse.json(notifications({ max_attempts: 9 }, 4)),
      ),
    ]);
    await actor.selectOptions(
      await screen.findByLabelText("Notice", {}, LOAD),
      DEPRECATED,
    );
    await actor.click(saveButton());
    await waitFor(() => {
      expect(screen.getByLabelText("Maximum attempts")).toHaveValue("9");
    });
    expect(screen.getByLabelText("Notice")).toHaveValue(DEPRECATED);
  });
});

describe("the test email dialog", () => {
  async function openDialog(answer: () => Response) {
    const actor = userEvent.setup();
    open({}, [http.post(TEST_EMAIL, () => answer())]);
    await screen.findByLabelText("Subject", {}, LOAD);
    await actor.click(screen.getByRole("button", { name: "Send test email" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Send a test email",
    });
    await actor.type(
      within(dialog).getByLabelText("Send to"),
      "me@example.com",
    );
    return { actor, dialog };
  }

  const sendFrom = (dialog: HTMLElement) =>
    within(dialog).getByRole("button", { name: "Send test email" });

  it.each([
    ["sent", "Test email sent"],
    ["refused", "Test email refused"],
    ["unavailable", "Mail is unavailable"],
    ["not-configured", "Email is not configured"],
  ])("words the %s outcome", async (outcome, title) => {
    const { actor, dialog } = await openDialog(() =>
      HttpResponse.json({ outcome }),
    );
    await actor.click(sendFrom(dialog));
    expect(await within(dialog).findByText(title)).toBeVisible();
  });

  it("sends on Enter in the address field and puts a /to 422 on the field", async () => {
    const { actor, dialog } = await openDialog(() =>
      problem(422, "validation-error", {
        errors: [{ pointer: "/to", detail: "This address is not allowed." }],
      }),
    );
    await actor.type(within(dialog).getByLabelText("Send to"), "{Enter}");
    expect(
      await within(dialog).findByText("This address is not allowed."),
    ).toBeVisible();
  });

  it("shows a 422 without a /to pointer in a problem alert", async () => {
    const { actor, dialog } = await openDialog(() =>
      problem(422, "validation-error", {
        errors: [{ pointer: "/other", detail: "Elsewhere." }],
      }),
    );
    await actor.click(sendFrom(dialog));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Elsewhere.",
    );
  });

  it.each([
    [{}, /Try again later\./],
    [{ retry_after_s: 30 }, /Try again in 30 seconds\./],
  ])("words the wait of a 429 (%j)", async (extra, wait) => {
    const { actor, dialog } = await openDialog(() =>
      problem(429, "too-many-attempts", extra),
    );
    await actor.click(sendFrom(dialog));
    expect(await within(dialog).findByText(wait)).toBeVisible();
  });

  it("refuses an answer without a body, then Close empties the dialog", async () => {
    const { actor, dialog } = await openDialog(
      () => new HttpResponse(null, { status: 204 }),
    );
    await actor.click(sendFrom(dialog));
    expect(
      await within(dialog).findByText("Something went wrong"),
    ).toBeVisible();
    await actor.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await actor.click(screen.getByRole("button", { name: "Send test email" }));
    const again = await screen.findByRole("dialog", {
      name: "Send a test email",
    });
    expect(within(again).getByLabelText("Send to")).toHaveValue("");
    await actor.click(
      within(again).getByRole("button", { name: "Close dialog" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});
