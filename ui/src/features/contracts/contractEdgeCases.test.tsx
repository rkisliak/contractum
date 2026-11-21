// Contract screens on answers the happy-path tests do not send: bodiless (204) lists and creates,
// a document that loads after the comparison, a report whose document fails, metadata without a
// sunset date, a version page with nothing Latest, a rule set paused or without a folder kind, a
// sunset refusal without detail and a draft without a declared version. Plus the findings panel and
// the document status reader on their remaining shapes.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, type RequestHandler } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import {
  CONTRACT_ALL,
  CONTRACT_API,
  contractFolders,
  contractHandlers,
  makeContract,
  makeDraft,
  makeVersion,
  problemResponse,
  textDocument,
} from "@/test/contracts-fixtures";
import { http, HttpResponse } from "@/test/msw";
import { renderApp } from "@/test/renderApp";
import { FindingsPanel } from "./FindingsPanel";
import { documentStatus } from "./PublishDialog";
import type { UnifiedFinding } from "./report";

const noContent = () => new HttpResponse(null, { status: 204 });
const FOLDER_API = "*/api/v1/contracts/folders/ledger-id";

afterEach(() => {
  tokenStore.clear();
});

function page(
  path: string,
  handlers: RequestHandler[],
  options: Parameters<typeof contractHandlers>[0] = {},
) {
  return renderApp(path, {
    folders: contractFolders,
    permissions: CONTRACT_ALL,
    handlers: [...handlers, ...contractHandlers(options)],
  });
}

function finding(overrides: Partial<UnifiedFinding> = {}): UnifiedFinding {
  return {
    id: "f-0",
    group: "warning",
    severity: "Warning",
    source: "platform-core v1",
    ruleKey: "has-owner",
    message: "Name an owner.",
    pointer: null,
    mode: "warn",
    paused: false,
    ...overrides,
  };
}

describe("findings panel", () => {
  it("marks a paused platform finding as not blocking", () => {
    render(<FindingsPanel findings={[finding({ paused: true })]} />);
    expect(screen.getByText("paused, does not block")).toBeVisible();
    expect(screen.getByText("location unavailable")).toBeVisible();
  });

  it("says stale results had no findings, with the default revalidate label", async () => {
    const user = userEvent.setup();
    const onRevalidate = vi.fn();
    render(
      <FindingsPanel findings={[]} stale={{ revision: 4, onRevalidate }} />,
    );
    await user.click(screen.getByRole("button", { name: "Save and validate" }));
    expect(onRevalidate).toHaveBeenCalledTimes(1);
    await user.click(
      screen.getByRole("button", { name: "Show previous findings" }),
    );
    expect(
      screen.getByText("The earlier results had no findings."),
    ).toBeVisible();
  });

  it("offers no revalidate action without a handler", () => {
    render(<FindingsPanel findings={[finding()]} stale={{ revision: 2 }} />);
    expect(
      screen.getByText(/These findings are for revision 2\./),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: /validate/i })).toBeNull();
  });
});

describe("documentStatus", () => {
  it("reads a number, a boolean or an object status as text", () => {
    expect(documentStatus("status: 3\n")).toBe("3");
    expect(documentStatus("status: true\n")).toBe("true");
    expect(documentStatus("status:\n  phase: live\n")).toBe('{"phase":"live"}');
  });
});

describe("bodiless answers", () => {
  it("shows a bodiless contract list as a problem", async () => {
    renderApp("/contracts", {
      folders: contractFolders,
      permissions: CONTRACT_ALL,
      handlers: [http.get("*/api/v1/contracts/contracts", noContent)],
    });
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("table", { name: "Contracts" })).toBeNull();
  });

  it("shows a bodiless diff as a problem", async () => {
    page("/contracts/c-1/diff?from=1.0.0&to=latest", [
      http.get(`${CONTRACT_API}/diff`, noContent),
      http.get(`${CONTRACT_API}/versions/:ref`, () =>
        textDocument("{}", "application/json"),
      ),
    ]);
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByText(/Required bump/)).toBeNull();
  });

  it("reads bodiless rule set lists as empty", async () => {
    page("/contracts/c-1/rule-sets", [
      http.get(`${CONTRACT_API}/rule-sets`, noContent),
      http.get(
        "*/api/v1/validation/folders/ledger-id/available-rule-sets",
        noContent,
      ),
    ]);
    expect(await screen.findByText("No attached rule sets")).toBeVisible();
    expect(
      await screen.findByText("No mandatory rule sets apply at this folder."),
    ).toBeVisible();
  });

  it("reads a bodiless subscriber page as empty", async () => {
    page("/contracts/c-1/subscribers", [
      http.get(`${CONTRACT_API}/subscribers`, noContent),
    ]);
    expect(await screen.findByText("No subscribers")).toBeVisible();
  });

  it("shows bodiless version metadata as a problem", async () => {
    page("/contracts/c-1/versions/1.0.0", [
      http.get(`${CONTRACT_API}/versions/1.0.0/metadata`, noContent),
    ]);
    expect(await screen.findByRole("alert")).toBeVisible();
  });

  it("stays on the drafts tab when a new draft answers without a body", async () => {
    const user = userEvent.setup();
    let created = 0;
    const { router } = page(
      "/contracts/c-1/drafts",
      [
        http.post(`${CONTRACT_API}/drafts`, () => {
          created += 1;
          return noContent();
        }),
      ],
      { drafts: [makeDraft("d-2", { declared_version: null })] },
    );
    await user.click(await screen.findByRole("button", { name: "New draft" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Create a draft",
    });
    // A draft without a declared version is offered by its revision alone.
    expect(
      await within(dialog).findByRole("option", { name: "Draft (revision 1)" }),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole("button", { name: "Create draft" }),
    );
    await waitFor(() => {
      expect(created).toBe(1);
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(router.state.location.pathname).toBe("/contracts/c-1/drafts");
  });
});

describe("new contract on bodiless creates", () => {
  const sources: [
    string,
    RequestHandler[],
    (user: ReturnType<typeof userEvent.setup>) => Promise<void>,
  ][] = [
    [
      "ODCS starter",
      [],
      async (user) => {
        await user.type(screen.getByRole("textbox", { name: "Name" }), "X");
      },
    ],
    ["Blank document", [], () => Promise.resolve()],
    [
      "From a template",
      [
        http.get(`${FOLDER_API}/available-templates`, () =>
          HttpResponse.json({
            items: [
              {
                template_id: "t-1",
                name: "Batch",
                folder_id: "ledger-id",
                latest_version_no: 1,
                version_nos: [1],
              },
            ],
            next_cursor: null,
          }),
        ),
        http.get(
          `${FOLDER_API}/available-templates/:id/versions/:no/document`,
          () => textDocument("kind: DataContract\n"),
        ),
      ],
      async (user) => {
        await user.selectOptions(
          await screen.findByRole("combobox", { name: "Template" }),
          "t-1",
        );
      },
    ],
    [
      "From another contract",
      [
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({ items: [makeContract()], next_cursor: null }),
        ),
      ],
      async (user) => {
        await user.selectOptions(
          await screen.findByRole("combobox", { name: "Source contract" }),
          "c-1",
        );
        await screen.findByRole("option", { name: "Version 1.0.0" });
        await user.selectOptions(
          screen.getByRole("combobox", { name: "Version or draft" }),
          "v:1.0.0",
        );
      },
    ],
  ];

  it.each(sources)(
    "reports a create that answers without a body (%s)",
    async (label, handlers, fill) => {
      const user = userEvent.setup();
      const { router } = page("/contracts/new?folder=ledger-id", [
        ...handlers,
        http.post(`${FOLDER_API}/contracts*`, noContent),
      ]);
      await user.click(
        await screen.findByRole("radio", { name: new RegExp(`^${label}`) }),
      );
      await fill(user);
      await user.click(screen.getByRole("button", { name: "Create contract" }));
      expect(await screen.findByRole("alert")).toBeVisible();
      expect(router.state.location.pathname).toBe("/contracts/new");
    },
  );

  it("offers no source contracts when the picker list has no body", async () => {
    const user = userEvent.setup();
    page("/contracts/new?folder=ledger-id", [
      http.get("*/api/v1/contracts/contracts", noContent),
    ]);
    await user.click(
      await screen.findByRole("radio", { name: /^From another contract/ }),
    );
    const select = screen.getByRole("combobox", { name: "Source contract" });
    expect(within(select).getAllByRole("option")).toHaveLength(1);
  });
});

describe("diff and version page timing", () => {
  it("shows a loading comparison until both documents arrive", async () => {
    page("/contracts/c-1/diff?from=1.0.0&to=latest", [
      http.get(`${CONTRACT_API}/diff`, () =>
        HttpResponse.json({
          changes: [],
          changes_total: 0,
          classifier_version: "1",
          counts: {},
          next_cursor: null,
          required_bump: "none",
          truncated: false,
        }),
      ),
      http.get(`${CONTRACT_API}/versions/:ref`, async () => {
        await delay("infinite");
        return textDocument("{}", "application/json");
      }),
    ]);
    expect(
      await screen.findByText("The two documents are equivalent: no changes."),
    ).toBeVisible();
    expect(screen.getByText("Loading documents")).toBeInTheDocument();
  });

  it("lists the stored report's findings when the version document fails", async () => {
    const user = userEvent.setup();
    page(
      "/contracts/c-1/versions/1.0.0",
      [
        http.get(`${CONTRACT_API}/versions/1.0.0/metadata`, () =>
          HttpResponse.json(
            makeVersion("1.0.0", {
              status: "older",
              deprecated: true,
              deprecated_at: "2026-11-05T00:00:00Z",
            }),
          ),
        ),
        http.get(`${CONTRACT_API}/versions/1.0.0/validation-report`, () =>
          HttpResponse.json({
            run_id: "run-1",
            report: {
              findings: [
                {
                  source: "rule",
                  severity: "warning",
                  message: "Set a domain.",
                  location: { pointer: "/domain" },
                  blocking: false,
                },
              ],
              summary: { publishable: true, blocking_count: 0 },
            },
            validated_at: "2026-11-02T10:00:00Z",
            engine_version: "engine-1",
            pins: [],
          }),
        ),
        http.get(`${CONTRACT_API}/versions/1.0.0`, () =>
          problemResponse(500, "internal"),
        ),
      ],
      { contract: makeContract({ latest_version: null }) },
    );
    await user.click(
      await screen.findByRole("tab", { name: "Validation report" }),
    );
    const findings = await screen.findByRole("region", {
      name: "Findings at publish",
    });
    expect(within(findings).getByText("Set a domain.")).toBeVisible();
    // Without the document there is nothing to locate and no editor.
    expect(
      within(findings).queryByRole("button", { name: /Go to line/ }),
    ).toBeNull();
    expect(
      screen.queryByRole("textbox", { name: "Version 1.0.0 document" }),
    ).toBeNull();

    await user.click(screen.getByRole("tab", { name: "Metadata" }));
    expect(
      await within(screen.getByRole("tabpanel")).findByText(
        /^Yes, since .*; sunset not set$/,
      ),
    ).toBeVisible();
  });
});

describe("rule sets and version actions wording", () => {
  it("words a paused mandatory version and a set without a folder kind", async () => {
    const user = userEvent.setup();
    const set = (id: string, overrides: Record<string, unknown>) => ({
      id,
      scope: "folder",
      folder_id: "acme-id",
      folder_kind: null,
      name: id,
      description: null,
      latest_version: 1,
      latest_active_version: 1,
      mandatory: false,
      versions: [
        {
          version_no: 1,
          content_sha256: "a",
          rule_keys: ["k"],
          deprecated_at: null,
          enforce_at: null,
          published_at: "2026-11-01T00:00:00Z",
          published_by: "local:admin",
        },
      ],
      resolved: [],
      ...overrides,
    });
    page("/contracts/c-1/rule-sets", [
      http.get(`${CONTRACT_API}/rule-sets`, () =>
        HttpResponse.json({ items: [], attachments_revision: 0 }),
      ),
      http.get(
        "*/api/v1/validation/folders/ledger-id/available-rule-sets",
        () =>
          HttpResponse.json({
            items: [
              set("platform-core", {
                scope: "platform",
                mandatory: true,
                resolved: [
                  {
                    version_no: 1,
                    mode: "warn",
                    enforce_at: null,
                    paused: true,
                  },
                ],
              }),
              set("org-extras", {}),
            ],
          }),
      ),
    ]);
    expect(await screen.findByText("v1 paused (warns only)")).toBeVisible();
    await user.click(
      await screen.findByRole("button", { name: "Attach rule set" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Attach a rule set",
    });
    expect(
      within(dialog).getByRole("option", { name: "org-extras (folder)" }),
    ).toBeInTheDocument();
  });

  it("asks for a date from today onwards when a sunset refusal has no detail", async () => {
    const user = userEvent.setup();
    page(
      "/contracts/c-1/versions",
      [
        http.post(`${CONTRACT_API}/versions/1.0.0/deprecate`, () =>
          problemResponse(422, "invalid-sunset-date", { detail: null }),
        ),
      ],
      { versions: [makeVersion("1.0.0", { status: "latest" })] },
    );
    await user.click(
      await screen.findByRole("button", { name: "Actions for 1.0.0" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Deprecate version" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Deprecate 1.0.0?",
    });
    const tomorrow = new Date(Date.now() + 86_400_000)
      .toISOString()
      .slice(0, 10);
    await user.type(
      within(dialog).getByLabelText("Sunset date (UTC)"),
      tomorrow,
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Deprecate 1.0.0" }),
    );
    expect(
      await within(dialog).findByText("Choose a date from today onwards."),
    ).toBeVisible();
  });
});
