// The contract tabs below the overview through the real router: Rule sets (attach, detach,
// mandatory list), Drafts (list, new draft, discard), Subscribers (paging, remove with a reason),
// Versions (the published banner, the per-version actions menu with every lifecycle write) and
// one version's page (document formats, downloads, the stored gate report and metadata).
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder } from "@/test/renderApp";

vi.setConfig({ testTimeout: 30000 });

const API = "*/api/v1/contracts/contracts/c-1";

const ledger = makeFolder({
  id: "ledger-id",
  kind: "team",
  slug: "ledger",
  display_name: "Ledger",
  path: "ledger",
});
const folders = [rootFolder, ledger];

const ALL = [
  "contract:read",
  "contract:read-metadata",
  "contract:update",
  "contract:publish",
  "contract:delete",
];
const READER = ["contract:read", "contract:read-metadata"];

const contract = {
  id: "c-1",
  folder_id: "ledger-id",
  organization_id: "ledger-id",
  odcs_id: "urn:acme:ledger:orders",
  notation: "odcs",
  name: "Orders",
  status: "active",
  api_version: "v3.1.0",
  domain: "payments",
  tags: [],
  media_type: "application/yaml",
  content_sha256: null,
  size_bytes: null,
  content_redacted: false,
  template: null,
  origin: null,
  version: { number: 2, state: "draft", deprecated: false },
  latest_version: { version_id: "v-1.0.0", semver: "1.0.0" },
  created_by: "local:admin",
  created_at: "2026-11-01T10:00:00Z",
  updated_by: "local:admin",
  updated_at: "2026-11-02T10:00:00Z",
};

function version(semver: string, overrides: Record<string, unknown> = {}) {
  return {
    version_id: `v-${semver}`,
    contract_id: "c-1",
    semver,
    status: "older",
    lifecycle: "published",
    deprecated: false,
    deprecated_at: null,
    sunset_date: null,
    content_sha256: `sha-${semver}`,
    etag: `"e-${semver}"`,
    size_bytes: 320,
    media_type: "application/yaml",
    odcs_api_version: "v3.1.0",
    published_at: "2026-11-02T10:00:00Z",
    published_by: "local:11111111-2222",
    fetch_url: `/api/v1/contracts/contracts/c-1/versions/${semver}`,
    validation_run_id: null,
    pinned_rule_sets: null,
    engine_version: null,
    schema_sha: null,
    ...overrides,
  };
}

function draft(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    contract_id: "c-1",
    revision: 1,
    etag: `"${id}-r1"`,
    declared_version: "1.1.0",
    base_version_id: "v-1.0.0",
    odcs_api_version: "v3.1.0",
    media_type: "application/yaml",
    content_sha256: "x",
    size_bytes: 10,
    name: "Orders",
    created_by: "local:admin",
    created_at: "2026-11-02T10:00:00Z",
    updated_by: "local:bob",
    updated_at: "2026-11-02T10:00:00Z",
    ...overrides,
  };
}

const DOC =
  "apiVersion: v3.1.0\nkind: DataContract\nid: urn:acme:ledger:orders\nversion: 1.0.0\n";

function problem(
  status: number,
  slug: string,
  extra: Record<string, unknown> = {},
) {
  return HttpResponse.json(
    {
      type: `urn:dc:problem:${slug}`,
      title: `Title of ${slug}`,
      status,
      detail: `${slug} detail`,
      ...extra,
    },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

/** A UTC date `days` from today: the sunset field refuses dates before today natively. */
function daysAhead(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
}
const REFUSED_SUNSET = daysAhead(20);
const SUNSET = daysAhead(90);
const PREFILLED_SUNSET = daysAhead(60);

const contractHandler = http.get(API, () => HttpResponse.json(contract));

afterEach(() => {
  tokenStore.clear();
  vi.restoreAllMocks();
});

/** Captures `saveText` downloads (jsdom has no object URLs and no navigation). */
function captureDownloads() {
  const saved: { name: string; href: string }[] = [];
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:test"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    saved.push({ name: this.download, href: this.href });
  });
  return saved;
}

// ---------------------------------------------------------------- Rule sets tab

function availableSet(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    scope: "folder",
    folder_id: "ledger-id",
    folder_kind: "team",
    name: id,
    description: null,
    latest_version: 2,
    latest_active_version: 2,
    mandatory: false,
    versions: [
      {
        version_no: 1,
        content_sha256: "a",
        rule_keys: ["k"],
        deprecated_at: "2026-11-03T00:00:00Z",
        enforce_at: null,
        published_at: "2026-11-01T00:00:00Z",
        published_by: "local:admin",
      },
      {
        version_no: 2,
        content_sha256: "b",
        rule_keys: ["k"],
        deprecated_at: null,
        enforce_at: null,
        published_at: "2026-11-02T00:00:00Z",
        published_by: "local:admin",
      },
    ],
    resolved: [],
    ...overrides,
  };
}

const attachedItems = [
  {
    rule_set_id: "rs-latest",
    rule_set_name: "naming",
    version: "latest",
    attached_at: "2026-11-03T10:00:00Z",
    attached_by: "local:aaaaaaaa-bbbb",
  },
  {
    rule_set_id: "rs-pinned",
    rule_set_name: "pii",
    version: 3,
    attached_at: "2026-11-03T10:00:00Z",
    attached_by: "plain",
  },
];

const availableItems = [
  availableSet("platform-core", {
    scope: "platform",
    folder_id: null,
    folder_kind: null,
    mandatory: true,
    description: "Platform baseline",
    resolved: [
      { version_no: 1, mode: "enforce", enforce_at: null },
      { version_no: 2, mode: "warn", enforce_at: "2026-12-01T00:00:00Z" },
    ],
  }),
  availableSet("team-must", {
    mandatory: true,
    folder_kind: null,
    resolved: [{ version_no: 2, mode: "warn", enforce_at: null }],
  }),
  availableSet("quality"),
  availableSet("gone", { latest_active_version: null }),
  availableSet("rs-latest"),
];

describe("contract rule sets tab", () => {
  it("lists attached and mandatory sets, attaches a pinned version and detaches", async () => {
    const user = userEvent.setup();
    let items = [...attachedItems];
    let putBody: unknown = null;
    let deleted = "";
    renderApp("/contracts/c-1/rule-sets", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/rule-sets`, () =>
          HttpResponse.json({ items, attachments_revision: 1 }),
        ),
        http.get(
          "*/api/v1/validation/folders/ledger-id/available-rule-sets",
          () => HttpResponse.json({ items: availableItems }),
        ),
        http.put(`${API}/rule-sets/quality`, async ({ request }) => {
          putBody = await request.json();
          items = [
            ...items,
            {
              rule_set_id: "quality",
              rule_set_name: "quality",
              version: 2,
              attached_at: "2026-11-04T10:00:00Z",
              attached_by: "local:admin",
            },
          ];
          return HttpResponse.json({ status: "attached" });
        }),
        http.delete(`${API}/rule-sets/:id`, ({ params }) => {
          deleted = String(params.id);
          items = items.filter((item) => item.rule_set_id !== params.id);
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    const table = await screen.findByRole("table", {
      name: "Attached rule sets",
    });
    expect(within(table).getByRole("link", { name: "naming" })).toHaveAttribute(
      "href",
      "/rule-sets/rs-latest",
    );
    expect(within(table).getByText("Tracks latest")).toBeVisible();
    expect(within(table).getByText("Version 3")).toBeVisible();
    expect(within(table).getByText("local:aaaaaaaa")).toBeVisible();
    expect(within(table).getByText("plain")).toBeVisible();

    // Mandatory here: scope badge and each resolved version's server mode.
    expect(await screen.findByText("platform-core")).toBeVisible();
    expect(screen.getByText("Platform")).toBeVisible();
    expect(screen.getByText("Platform baseline")).toBeVisible();
    expect(screen.getByText("v1 enforced")).toBeVisible();
    // platform-core's v2 warns until a date; team-must's v2 has no date yet.
    expect(screen.getAllByText(/^v2 warns until /)).toHaveLength(2);
    expect(screen.getByText("team-must")).toBeVisible();
    expect(screen.getByText("Folder")).toBeVisible();
    expect(screen.getByText("v2 warns until later")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Attach rule set" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Attach a rule set",
    });
    const setSelect = within(dialog).getByRole("combobox", {
      name: "Rule set",
    });
    // Only optional, active and not yet attached sets are offered.
    expect(
      within(setSelect)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Choose a rule set", "quality (team)"]);
    const versionSelect = within(dialog).getByRole("combobox", {
      name: "Version",
    });
    expect(versionSelect).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "Attach rule set" }),
    ).toBeDisabled();
    await user.selectOptions(setSelect, "quality");
    expect(versionSelect).toBeEnabled();
    // The deprecated version 1 is not offered.
    expect(
      within(versionSelect)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Track latest", "Version 2"]);
    await user.selectOptions(versionSelect, "2");
    await user.click(
      within(dialog).getByRole("button", { name: "Attach rule set" }),
    );
    expect(await screen.findByText("quality is attached.")).toBeVisible();
    expect(putBody).toEqual({ version: 2 });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(
      await within(table).findByRole("link", { name: "quality" }),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Detach pii" }));
    const confirm = await screen.findByRole("alertdialog", {
      name: "Detach pii?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Detach pii" }),
    );
    expect(await screen.findByText("pii is detached.")).toBeVisible();
    expect(deleted).toBe("rs-pinned");
    await waitFor(() => {
      expect(within(table).queryByRole("link", { name: "pii" })).toBeNull();
    });
  });

  it("says when an attached set is already mandatory, and shows an attach failure in the dialog", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    let fail = false;
    renderApp("/contracts/c-1/rule-sets", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/rule-sets`, () =>
          HttpResponse.json({ items: [], attachments_revision: 0 }),
        ),
        http.get(
          "*/api/v1/validation/folders/ledger-id/available-rule-sets",
          () => HttpResponse.json({ items: [availableSet("quality")] }),
        ),
        http.put(`${API}/rule-sets/quality`, async ({ request }) => {
          bodies.push(await request.json());
          return fail
            ? problem(409, "rule-set-deprecated")
            : HttpResponse.json({ status: "mandatory" });
        }),
      ],
    });
    expect(await screen.findByText("No attached rule sets")).toBeVisible();
    expect(
      await screen.findByText("No mandatory rule sets apply at this folder."),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Attach rule set" }));
    let dialog = await screen.findByRole("dialog");
    await user.selectOptions(
      within(dialog).getByRole("combobox", { name: "Rule set" }),
      "quality",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Attach rule set" }),
    );
    expect(
      await screen.findByText(
        "quality is already mandatory here, so it always applies.",
      ),
    ).toBeVisible();
    expect(bodies).toEqual([{ version: "latest" }]);

    fail = true;
    await user.click(screen.getByRole("button", { name: "Attach rule set" }));
    dialog = await screen.findByRole("dialog");
    await user.selectOptions(
      within(dialog).getByRole("combobox", { name: "Rule set" }),
      "quality",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Attach rule set" }),
    );
    expect(
      await within(dialog).findByText("rule-set-deprecated detail"),
    ).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("tells the editor when nothing is left to attach", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/rule-sets", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/rule-sets`, () =>
          HttpResponse.json({ items: [], attachments_revision: 0 }),
        ),
        http.get(
          "*/api/v1/validation/folders/ledger-id/available-rule-sets",
          () => HttpResponse.json({ items: [] }),
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Attach rule set" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(
        "Every available rule set is already attached or mandatory here.",
      ),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Attach rule set" }),
    ).toBeDisabled();
  });

  it("hides attach and detach without contract:update", async () => {
    renderApp("/contracts/c-1/rule-sets", {
      folders,
      permissions: READER,
      handlers: [
        contractHandler,
        http.get(`${API}/rule-sets`, () =>
          HttpResponse.json({ items: attachedItems, attachments_revision: 1 }),
        ),
        http.get(
          "*/api/v1/validation/folders/ledger-id/available-rule-sets",
          () => HttpResponse.json({ items: [] }),
        ),
      ],
    });
    expect(await screen.findByRole("link", { name: "naming" })).toBeVisible();
    // Wait for the capabilities: the attach button never appears.
    await screen.findByText("No mandatory rule sets apply at this folder.");
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "Attach rule set" }),
      ).toBeNull();
    });
    expect(screen.queryByRole("button", { name: /^Detach/ })).toBeNull();
  });

  it("shows both lists' problems, with a retry for the attached list", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/contracts/c-1/rule-sets", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/rule-sets`, () => {
          calls += 1;
          return problem(500, "attachments-broken");
        }),
        http.get(
          "*/api/v1/validation/folders/ledger-id/available-rule-sets",
          () => problem(503, "validation-down"),
        ),
      ],
    });
    expect(await screen.findByText("attachments-broken detail")).toBeVisible();
    expect(await screen.findAllByRole("alert")).not.toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    await waitFor(() => {
      expect(calls).toBe(2);
    });
  });
});

// ---------------------------------------------------------------- Drafts tab

describe("contract drafts tab", () => {
  const draftList = [
    draft("d-1"),
    draft("d-2", {
      declared_version: null,
      base_version_id: "v-gone",
      media_type: "application/json",
      revision: 4,
      etag: '"d-2-r4"',
    }),
    draft("d-3", { declared_version: "2.0.0", base_version_id: null }),
  ];

  it("lists drafts and discards one with If-Match", async () => {
    const user = userEvent.setup();
    let items = [...draftList];
    let ifMatch: string | null = null;
    renderApp("/contracts/c-1/drafts", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/drafts`, () => HttpResponse.json({ items })),
        http.get(`${API}/versions`, () =>
          HttpResponse.json({
            items: [version("1.0.0", { status: "latest" })],
          }),
        ),
        http.delete(`${API}/drafts/d-2`, ({ request }) => {
          ifMatch = request.headers.get("If-Match");
          items = items.filter((item) => item.id !== "d-2");
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    const link = await screen.findByRole("link", { name: "1.1.0" });
    expect(link).toHaveAttribute("href", "/contracts/c-1/drafts/d-1");
    const rowOf = (name: string) =>
      screen.getByRole("link", { name }).closest("tr") as HTMLElement;
    await waitFor(() => {
      expect(within(rowOf("1.1.0")).getByText("1.0.0")).toBeVisible();
    });
    expect(within(rowOf("1.1.0")).getByText("YAML")).toBeVisible();
    expect(within(rowOf("1.1.0")).getByText("local:bob")).toBeVisible();
    expect(within(rowOf("No version")).getByText("A version")).toBeVisible();
    expect(within(rowOf("No version")).getByText("JSON")).toBeVisible();
    expect(within(rowOf("No version")).getByText("4")).toBeVisible();
    expect(within(rowOf("2.0.0")).getByText("Not set")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Updated" }));
    expect(
      screen.getByRole("columnheader", { name: "Updated" }),
    ).toHaveAttribute("aria-sort", "ascending");

    await user.click(screen.getByRole("button", { name: "Discard draft d-2" }));
    const confirm = await screen.findByRole("alertdialog", {
      name: "Discard draft ?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Discard draft" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "No version" })).toBeNull();
    });
    expect(ifMatch).toBe('"d-2-r4"');
  });

  it("creates a draft from the newest version by default and opens it", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    const { router } = renderApp("/contracts/c-1/drafts", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/drafts`, () =>
          HttpResponse.json({ items: [draftList[0]] }),
        ),
        http.get(`${API}/versions`, () =>
          HttpResponse.json({
            items: [version("1.1.0"), version("1.0.0", { status: "latest" })],
          }),
        ),
        http.post(`${API}/drafts`, async ({ request }) => {
          body = await request.json();
          return HttpResponse.json({ draft: draft("d-1") }, { status: 201 });
        }),
        http.get(`${API}/drafts/d-1`, () => HttpResponse.json(draft("d-1"))),
        http.get(
          `${API}/drafts/d-1/document`,
          () =>
            new HttpResponse(DOC, {
              headers: { "Content-Type": "application/yaml" },
            }),
        ),
      ],
    });
    await user.click(await screen.findByRole("button", { name: "New draft" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Create a draft",
    });
    const source = within(dialog).getByRole("combobox", { name: "Start from" });
    await waitFor(() => {
      expect(
        within(source)
          .getAllByRole("option")
          .map((option) => option.textContent),
      ).toEqual(["Version 1.1.0", "Version 1.0.0", "Draft 1.1.0 (revision 1)"]);
    });
    expect(source).toHaveValue("v:1.1.0");
    await user.click(
      within(dialog).getByRole("button", { name: "Create draft" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/c-1/drafts/d-1");
    });
    expect(body).toEqual({ bump: "minor", from_version: "1.1.0" });
  });

  it("creates a draft from another draft with a major bump, and shows a refusal", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/contracts/c-1/drafts", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/drafts`, () =>
          HttpResponse.json({ items: [draftList[0]] }),
        ),
        http.get(`${API}/versions`, () =>
          HttpResponse.json({ items: [version("1.0.0")] }),
        ),
        http.post(`${API}/drafts`, async ({ request }) => {
          bodies.push(await request.json());
          return problem(409, "draft-limit");
        }),
      ],
    });
    await user.click(await screen.findByRole("button", { name: "New draft" }));
    const dialog = await screen.findByRole("dialog");
    const source = within(dialog).getByRole("combobox", { name: "Start from" });
    await waitFor(() => {
      expect(within(source).getAllByRole("option")).toHaveLength(2);
    });
    await user.selectOptions(source, "d:d-1");
    await user.selectOptions(
      within(dialog).getByRole("combobox", { name: "Version bump" }),
      "major",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Create draft" }),
    );
    expect(await within(dialog).findByText("draft-limit detail")).toBeVisible();
    expect(bodies).toEqual([{ bump: "major", from_draft_id: "d-1" }]);
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("shows the empty state, and a dialog with nothing to start from", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/drafts`, () => HttpResponse.json({ items: [] })),
        http.get(`${API}/versions`, () => HttpResponse.json({ items: [] })),
      ],
    });
    expect(await screen.findByText("No drafts")).toBeVisible();
    await user.click(await screen.findByRole("button", { name: "New draft" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByRole("button", { name: "Create draft" }),
    ).toBeDisabled();
  });

  it("hides New draft and Discard for a reader, and retries a failed list", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/contracts/c-1/drafts", {
      folders,
      permissions: READER,
      handlers: [
        contractHandler,
        http.get(`${API}/drafts`, () => {
          calls += 1;
          return calls === 1
            ? problem(500, "drafts-broken")
            : HttpResponse.json({ items: [draftList[0]] });
        }),
        http.get(`${API}/versions`, () => HttpResponse.json({ items: [] })),
      ],
    });
    expect(await screen.findByText("drafts-broken detail")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(await screen.findByRole("link", { name: "1.1.0" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "New draft" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Discard/ })).toBeNull();
  });
});

// ---------------------------------------------------------------- Subscribers tab

function card(id: string, overrides: Record<string, unknown> = {}) {
  return {
    subscription_id: `s-${id}`,
    card_id: id,
    name: `Card ${id}`,
    description: "",
    contacts: [],
    pinned_at: "2026-11-05T10:00:00Z",
    pinned_by: "local:cccccccc-dddd",
    ...overrides,
  };
}

describe("contract subscribers tab", () => {
  it("lists pinned cards per version, pages with the cursor and removes a subscription", async () => {
    const user = userEvent.setup();
    const cursors: (string | null)[] = [];
    let removeBody: unknown = null;
    let removed = false;
    renderApp("/contracts/c-1/subscribers", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/subscribers`, ({ request }) => {
          const url = new URL(request.url);
          const cursor = url.searchParams.get("cursor");
          cursors.push(cursor);
          expect(url.searchParams.get("limit")).toBe("50");
          if (cursor === "next-1")
            return HttpResponse.json({
              contract_id: "c-1",
              next_cursor: null,
              versions: [
                {
                  version_id: "v-0.9.0",
                  semver: "0.9.0",
                  deprecated: true,
                  sunset_date: null,
                  cards: [card("k-3")],
                },
              ],
            });
          return HttpResponse.json({
            contract_id: "c-1",
            next_cursor: "next-1",
            versions: [
              {
                version_id: "v-1.0.0",
                semver: "1.0.0",
                deprecated: true,
                sunset_date: "2027-01-31",
                cards: removed
                  ? [card("k-2")]
                  : [
                      card("k-1", {
                        description: "Nightly loads",
                        contacts: [
                          { kind: "email", name: "Ops", value: "ops@x.io" },
                          { kind: "url", name: null, value: "https://x.io" },
                        ],
                      }),
                      card("k-2"),
                    ],
              },
            ],
          });
        }),
        // The owner never reads the card's own subscriptions (no card access needed).
        http.get("*/api/v1/contracts/cards/:cardId/subscriptions", () => {
          throw new Error("the card's subscription list must not be read");
        }),
        http.post(`${API}/subscriptions/s-k-1/remove`, async ({ request }) => {
          removeBody = await request.json();
          removed = true;
          return HttpResponse.json({});
        }),
      ],
    });
    const link = await screen.findByRole("link", { name: "Card k-1" });
    expect(link).toHaveAttribute("href", "/cards/k-1");
    expect(screen.getByText("Deprecated · sunset 2027-01-31")).toBeVisible();
    const row = link.closest("tr") as HTMLElement;
    expect(within(row).getByText("Nightly loads")).toBeVisible();
    expect(
      within(row).getByText("Ops <ops@x.io>, <https://x.io>"),
    ).toBeVisible();
    expect(within(row).getByText("local:cccccccc")).toBeVisible();
    const other = screen
      .getByRole("link", { name: "Card k-2" })
      .closest("tr") as HTMLElement;
    expect(within(other).getByText("None")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(await screen.findByRole("link", { name: "Card k-3" })).toBeVisible();
    expect(
      screen.getByRole("heading", { name: /Version 0\.9\.0/ }),
    ).toHaveTextContent("Version 0.9.0 Deprecated");
    expect(cursors).toEqual([null, "next-1"]);
    expect(
      screen.queryByRole("button", { name: "Load more results" }),
    ).toBeNull();

    await user.click(
      within(row).getByRole("button", { name: "Remove subscription" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Remove Card k-1 from 1.0.0?",
    });
    // A reason is required before anything is sent.
    await user.click(
      within(dialog).getByRole("button", { name: "Remove subscription" }),
    );
    expect(await within(dialog).findByText("Enter a reason.")).toBeVisible();
    await user.type(
      within(dialog).getByRole("textbox", { name: "Reason" }),
      "  Moved to v2  ",
    );
    expect(within(dialog).queryByText("Enter a reason.")).toBeNull();
    await user.click(
      within(dialog).getByRole("button", { name: "Remove subscription" }),
    );
    expect(
      await screen.findByText("Card k-1 no longer subscribes to 1.0.0."),
    ).toBeVisible();
    expect(removeBody).toEqual({ reason: "Moved to v2" });
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "Card k-1" })).toBeNull();
    });
  });

  it("keeps the dialog open with the API problem when the remove is refused", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/subscribers", {
      folders,
      permissions: ["contract:read", "contract:publish"],
      handlers: [
        contractHandler,
        http.get(`${API}/subscribers`, () =>
          HttpResponse.json({
            contract_id: "c-1",
            next_cursor: null,
            versions: [
              {
                version_id: "v-1.0.0",
                semver: "1.0.0",
                deprecated: false,
                sunset_date: null,
                cards: [card("k-1")],
              },
            ],
          }),
        ),
        http.post(`${API}/subscriptions/s-k-1/remove`, () =>
          problem(404, "subscription-not-found"),
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Remove subscription" }),
    );
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByRole("textbox"), "gone");
    await user.click(
      within(dialog).getByRole("button", { name: "Remove subscription" }),
    );
    expect(
      await within(dialog).findByText("subscription-not-found detail"),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Keep subscription" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("shows the empty state, the error with retry, and no remove for a reader", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/contracts/c-1/subscribers", {
      folders,
      permissions: READER,
      handlers: [
        contractHandler,
        http.get(`${API}/subscribers`, () => {
          calls += 1;
          if (calls === 1) return problem(500, "subscribers-broken");
          if (calls === 2)
            return HttpResponse.json({
              contract_id: "c-1",
              next_cursor: null,
              versions: [],
            });
          return HttpResponse.json({});
        }),
      ],
    });
    expect(await screen.findByText("subscribers-broken detail")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(await screen.findByText("No subscribers")).toBeVisible();
  });

  it("hides Remove subscription without the publish right (update alone is not enough)", async () => {
    renderApp("/contracts/c-1/subscribers", {
      folders,
      permissions: [...READER, "contract:update"],
      handlers: [
        contractHandler,
        http.get(`${API}/subscribers`, () =>
          HttpResponse.json({
            contract_id: "c-1",
            next_cursor: null,
            versions: [
              {
                version_id: "v-1.0.0",
                semver: "1.0.0",
                deprecated: false,
                sunset_date: null,
                cards: [card("k-1")],
              },
            ],
          }),
        ),
      ],
    });
    expect(await screen.findByRole("link", { name: "Card k-1" })).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Remove subscription" }),
    ).toBeNull();
  });
});

// ---------------------------------------------------------------- Versions tab and actions

function versionsHandlers(
  items: Record<string, unknown>[],
  extra: RequestHandler[] = [],
) {
  return [
    contractHandler,
    ...extra,
    http.get(`${API}/versions`, () => HttpResponse.json({ items })),
  ];
}

const preview = version("1.1.0", { status: "preview" });
const latest = version("1.0.0", { status: "latest" });

async function openMenu(
  user: ReturnType<typeof userEvent.setup>,
  semver: string,
) {
  await user.click(
    await screen.findByRole("button", { name: `Actions for ${semver}` }),
  );
  return screen.findByRole("menu");
}

describe("contract versions tab", () => {
  it("names who published each version with one batched lookup, short id when unknown", async () => {
    const asked: string[][] = [];
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [
          { ...preview, published_by: "local:aaaaaaaa-1111" },
          { ...latest, published_by: "local:bbbbbbbb-2222" },
        ],
        [
          http.post(
            "*/api/v1/identity/principals/names",
            async ({ request }) => {
              const body = (await request.json()) as { ids: string[] };
              asked.push([...body.ids].sort());
              return HttpResponse.json({
                items: [
                  { id: "local:aaaaaaaa-1111", display_name: "Lena Ledger" },
                ],
              });
            },
          ),
        ],
      ),
    });
    expect(await screen.findByText("Lena Ledger")).toBeVisible();
    expect(screen.getByText("Lena Ledger")).toHaveAttribute(
      "title",
      "local:aaaaaaaa-1111",
    );
    expect(screen.getByText("local:bbbbbbbb")).toBeVisible();
    expect(asked).toEqual([["local:aaaaaaaa-1111", "local:bbbbbbbb-2222"]]);
  });

  it("shows the published-as-Preview banner, row facts and the fetch help", async () => {
    renderApp("/contracts/c-1/versions?published=1.1.0&status=preview", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers([
        { ...preview, size_bytes: 4096, media_type: "application/json" },
        { ...latest, published_at: null },
      ]),
    });
    expect(await screen.findByText("Published 1.1.0 as Preview")).toBeVisible();
    expect(
      screen.getByText(/It is above the Latest, so consumers reach it/),
    ).toBeVisible();
    const row = (await screen.findByRole("link", { name: "1.1.0" })).closest(
      "tr",
    ) as HTMLElement;
    expect(within(row).getByText("4.0 KiB")).toBeVisible();
    expect(within(row).getByText("JSON")).toBeVisible();
    expect(within(row).getByText("local:11111111")).toBeVisible();
    const older = screen
      .getByRole("link", { name: "1.0.0" })
      .closest("tr") as HTMLElement;
    expect(within(older).getByText("320 B")).toBeVisible();
    expect(within(older).getByText("Not recorded")).toBeVisible();
    expect(screen.getByText("…/versions/1.0.0")).toBeVisible();
    expect(
      screen.getByText(
        `${window.location.origin}/api/v1/contracts/contracts/c-1/versions/latest`,
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: /API documentation/ }),
    ).toHaveAttribute("href", "/api/v1/docs");
  });

  it("words the banner for a new Latest and for an unknown status", async () => {
    renderApp("/contracts/c-1/versions?published=2.0.0&status=latest", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers([latest]),
    });
    expect(await screen.findByText("Published 2.0.0 as Latest")).toBeVisible();
    expect(
      screen.getByText("Consumers fetching the Latest now receive it."),
    ).toBeVisible();
  });

  it("words the banner for an unknown status", async () => {
    renderApp("/contracts/c-1/versions?published=2.0.0&status=odd", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers([latest]),
    });
    expect(
      await screen.findByText("Published 2.0.0 as a new version"),
    ).toBeVisible();
  });

  it("shows the empty state with a link to drafts, and the example version", async () => {
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers([]),
    });
    expect(await screen.findByText("No published versions")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open drafts" })).toHaveAttribute(
      "href",
      "/contracts/c-1/drafts",
    );
    expect(screen.getByText("…/versions/1.0.0")).toBeVisible();
  });

  it("retries a failed versions list", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/versions`, () => {
          calls += 1;
          return calls === 1
            ? problem(500, "versions-broken")
            : HttpResponse.json({ items: [latest] });
        }),
      ],
    });
    expect(await screen.findByText("versions-broken detail")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(await screen.findByRole("link", { name: "1.0.0" })).toBeVisible();
  });
});

describe("version actions menu", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  it("offers reading actions only to a reader and none of the lifecycle writes", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: READER,
      handlers: versionsHandlers([preview, latest]),
    });
    const menu = await openMenu(user, "1.1.0");
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual([
      "View version",
      "Compare with Latest",
      "Copy fetch URL",
      "Copy Latest alias URL",
      "Copy curl example",
      "Download YAML",
      "Download JSON",
    ]);
  });

  it("offers no compare or promote on the Latest itself", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers([preview, latest]),
    });
    const menu = await openMenu(user, "1.0.0");
    const labels = within(menu)
      .getAllByRole("menuitem")
      .map((item) => item.textContent);
    expect(labels).not.toContain("Compare with Latest");
    expect(labels).not.toContain("Promote to Latest");
    expect(labels).toContain("Deprecate version");
    expect(labels).toContain("Unpublish version");
    expect(labels).not.toContain("Republish version");
  });

  it("opens the version page and the comparison with the Latest", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [preview, latest],
        [
          http.get(`${API}/versions/1.1.0/metadata`, () =>
            HttpResponse.json(preview),
          ),
          http.get(`${API}/versions/:ref`, () => new HttpResponse(DOC)),
          http.get(`${API}/diff`, () => problem(404, "not-found")),
          http.get(`${API}/drafts`, () => HttpResponse.json({ items: [] })),
        ],
      ),
    });
    await openMenu(user, "1.1.0");
    await user.click(screen.getByRole("menuitem", { name: "View version" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(
        "/contracts/c-1/versions/1.1.0",
      );
    });
    await user.click(
      await screen.findByRole("button", { name: "Actions for 1.1.0" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Compare with Latest" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/c-1/diff");
    });
    expect(router.state.location.search).toEqual({
      from: "latest",
      to: "1.1.0",
    });
  });

  it("copies the fetch URL, the Latest alias and a curl example", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers([preview, latest]),
    });
    const origin = window.location.origin;
    const fetchUrl = `${origin}/api/v1/contracts/contracts/c-1/versions/1.1.0`;

    await openMenu(user, "1.1.0");
    await user.click(screen.getByRole("menuitem", { name: "Copy fetch URL" }));
    expect(
      await screen.findByText("Fetch URL for 1.1.0 copied."),
    ).toBeVisible();
    expect(await navigator.clipboard.readText()).toBe(fetchUrl);

    await openMenu(user, "1.1.0");
    await user.click(
      screen.getByRole("menuitem", { name: "Copy Latest alias URL" }),
    );
    expect(await screen.findByText("Latest alias URL copied.")).toBeVisible();
    expect(await navigator.clipboard.readText()).toBe(
      `${origin}/api/v1/contracts/contracts/c-1/versions/latest`,
    );

    await openMenu(user, "1.1.0");
    await user.click(
      screen.getByRole("menuitem", { name: "Copy curl example" }),
    );
    expect(await screen.findByText("curl example copied.")).toBeVisible();
    expect(await navigator.clipboard.readText()).toBe(
      `curl -H "Authorization: Bearer $DC_TOKEN" -H "Accept: application/yaml" ${fetchUrl}`,
    );
  });

  it("explains when the clipboard is unavailable", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(
      new Error("denied"),
    );
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers([preview, latest]),
    });
    await openMenu(user, "1.1.0");
    await user.click(screen.getByRole("menuitem", { name: "Copy fetch URL" }));
    expect(
      await screen.findByText(
        "The clipboard is unavailable. Copy the value from the version page.",
      ),
    ).toBeVisible();
  });

  it("downloads YAML and JSON with Accept, and reports a failed download", async () => {
    const user = userEvent.setup();
    const saved = captureDownloads();
    const accepts: (string | null)[] = [];
    let fail = false;
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [preview, latest],
        [
          http.get(`${API}/versions/1.1.0`, ({ request }) => {
            accepts.push(request.headers.get("Accept"));
            if (fail) return problem(500, "boom");
            return new HttpResponse("{}", {
              headers: {
                "Content-Type": `${request.headers.get("Accept") ?? ""}; charset=utf-8`,
              },
            });
          }),
        ],
      ),
    });
    await openMenu(user, "1.1.0");
    await user.click(screen.getByRole("menuitem", { name: "Download YAML" }));
    await waitFor(() => {
      expect(saved.map((entry) => entry.name)).toEqual([
        "urn-acme-ledger-orders-1.1.0.yaml",
      ]);
    });
    await openMenu(user, "1.1.0");
    await user.click(screen.getByRole("menuitem", { name: "Download JSON" }));
    await waitFor(() => {
      expect(saved).toHaveLength(2);
    });
    expect(saved[1]?.name).toBe("urn-acme-ledger-orders-1.1.0.json");
    expect(accepts).toEqual(["application/yaml", "application/json"]);

    fail = true;
    await openMenu(user, "1.1.0");
    await user.click(screen.getByRole("menuitem", { name: "Download JSON" }));
    expect(
      await screen.findByText("Downloading 1.1.0 failed. Try again."),
    ).toBeVisible();
  });

  it("deprecates with a sunset date, refusing an empty or invalid date first", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [preview, latest],
        [
          http.post(`${API}/versions/1.1.0/deprecate`, async ({ request }) => {
            const body = (await request.json()) as { sunset_date: string };
            bodies.push(body);
            return body.sunset_date === REFUSED_SUNSET
              ? problem(422, "invalid-sunset-date", {
                  detail: "The sunset date is in the past.",
                })
              : HttpResponse.json(
                  version("1.1.0", {
                    status: "preview",
                    deprecated: true,
                    sunset_date: body.sunset_date,
                  }),
                );
          }),
        ],
      ),
    });
    await openMenu(user, "1.1.0");
    await user.click(
      screen.getByRole("menuitem", { name: "Deprecate version" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Deprecate 1.1.0?",
    });
    const dateInput = within(dialog).getByLabelText("Sunset date (UTC)");
    expect(dateInput).toHaveAttribute(
      "min",
      new Date().toISOString().slice(0, 10),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Deprecate 1.1.0" }),
    );
    expect(
      await within(dialog).findByText("Choose a sunset date."),
    ).toBeVisible();
    expect(bodies).toEqual([]);

    fireEvent.change(dateInput, { target: { value: REFUSED_SUNSET } });
    expect(within(dialog).queryByText("Choose a sunset date.")).toBeNull();
    await user.click(
      within(dialog).getByRole("button", { name: "Deprecate 1.1.0" }),
    );
    // The field shows the server's reason; the dialog shows the problem.
    expect(
      await within(dialog).findAllByText("The sunset date is in the past."),
    ).toHaveLength(2);

    fireEvent.change(dateInput, { target: { value: SUNSET } });
    await user.click(
      within(dialog).getByRole("button", { name: "Deprecate 1.1.0" }),
    );
    expect(
      await screen.findByText(`1.1.0 is deprecated; sunset ${SUNSET}.`),
    ).toBeVisible();
    expect(bodies).toEqual([
      { sunset_date: REFUSED_SUNSET },
      { sunset_date: SUNSET },
    ]);
  });

  it("changes the sunset date of a deprecated version, prefilled", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [
          version("1.1.0", {
            status: "preview",
            deprecated: true,
            deprecated_at: "2026-11-05T00:00:00Z",
            sunset_date: PREFILLED_SUNSET,
          }),
          latest,
        ],
        [
          http.post(`${API}/versions/1.1.0/deprecate`, async ({ request }) => {
            body = await request.json();
            return HttpResponse.json({});
          }),
        ],
      ),
    });
    expect(
      await screen.findByText(`Deprecated · sunset ${PREFILLED_SUNSET}`),
    ).toBeVisible();
    await openMenu(user, "1.1.0");
    await user.click(
      screen.getByRole("menuitem", { name: "Change sunset date" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Change the sunset date of 1.1.0?",
    });
    expect(within(dialog).getByLabelText("Sunset date (UTC)")).toHaveValue(
      PREFILLED_SUNSET,
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Change sunset date" }),
    );
    expect(
      await screen.findByText(
        `1.1.0 is deprecated; sunset ${PREFILLED_SUNSET}.`,
      ),
    ).toBeVisible();
    expect(body).toEqual({ sunset_date: PREFILLED_SUNSET });
  });

  it("explains an unpublish refused by subscriber pins, then unpublishes", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [preview, latest],
        [
          http.post(`${API}/versions/1.1.0/unpublish`, () => {
            calls += 1;
            return calls === 1
              ? problem(409, "version-pinned", {
                  reasons: [{ card_name: "Finance" }, { other: 1 }, "Ops", 7],
                })
              : HttpResponse.json({});
          }),
        ],
      ),
    });
    await openMenu(user, "1.1.0");
    await user.click(
      screen.getByRole("menuitem", { name: "Unpublish version" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Unpublish 1.1.0?",
    });
    expect(
      within(dialog).getByText(
        "It disappears for everyone without publish rights. The number 1.1.0 is never reused.",
      ),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Unpublish 1.1.0" }),
    );
    expect(
      await screen.findByText(
        "1.1.0 is pinned by subscriber cards: Finance, A card, Ops, A card. It was not unpublished.",
      ),
    ).toBeVisible();
    expect(within(dialog).getByText("version-pinned detail")).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Unpublish 1.1.0" }),
    );
    expect(await screen.findByText("1.1.0 is unpublished.")).toBeVisible();
    expect(calls).toBe(2);
  });

  it("words a pin refusal without named cards", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [preview, latest],
        [
          http.post(`${API}/versions/1.1.0/unpublish`, () =>
            problem(409, "version-pinned"),
          ),
        ],
      ),
    });
    await openMenu(user, "1.1.0");
    await user.click(
      screen.getByRole("menuitem", { name: "Unpublish version" }),
    );
    const dialog = await screen.findByRole("alertdialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Unpublish 1.1.0" }),
    );
    expect(
      await screen.findByText(
        "1.1.0 is pinned by subscriber cards. It was not unpublished.",
      ),
    ).toBeVisible();
  });

  it("republishes an unpublished version", async () => {
    const user = userEvent.setup();
    let called = false;
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [version("0.9.0", { lifecycle: "unpublished" }), latest],
        [
          http.post(`${API}/versions/0.9.0/republish`, () => {
            called = true;
            return HttpResponse.json({});
          }),
        ],
      ),
    });
    expect(await screen.findByText("Unpublished")).toBeVisible();
    const menu = await openMenu(user, "0.9.0");
    const labels = within(menu)
      .getAllByRole("menuitem")
      .map((item) => item.textContent);
    expect(labels).not.toContain("Unpublish version");
    expect(labels).not.toContain("Deprecate version");
    await user.click(
      screen.getByRole("menuitem", { name: "Republish version" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Republish 0.9.0?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Republish 0.9.0" }),
    );
    expect(await screen.findByText("0.9.0 is published again.")).toBeVisible();
    expect(called).toBe(true);
  });

  function publicSettings(requireReason: boolean) {
    return http.get("*/api/v1/identity/settings/public", () =>
      HttpResponse.json({
        banner: null,
        entra_default_tenant_id: null,
        entra_default_owner_folder_id: null,
        default_odcs_api_version: "v3.2.0",
        require_lifecycle_reason: requireReason,
        credential_max_ttl_days: 365,
        password_rules: {
          min_length: 12,
          require_upper: false,
          require_lower: false,
          require_digit: false,
          require_symbol: false,
        },
      }),
    );
  }

  it("sends an optional unpublish reason, and no body when there is none", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [preview, latest],
        [
          http.post(`${API}/versions/1.1.0/unpublish`, async ({ request }) => {
            const text = await request.text();
            bodies.push(text === "" ? null : JSON.parse(text));
            return HttpResponse.json({});
          }),
        ],
      ),
    });
    await openMenu(user, "1.1.0");
    await user.click(
      screen.getByRole("menuitem", { name: "Unpublish version" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Unpublish 1.1.0?",
    });
    expect(within(dialog).getByText("Optional")).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Unpublish 1.1.0" }),
    );
    expect(await screen.findByText("1.1.0 is unpublished.")).toBeVisible();
    expect(bodies).toEqual([null]);
  });

  it("requires a reason to unpublish while the platform setting is on", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [preview, latest],
        [
          publicSettings(true),
          http.post(`${API}/versions/1.1.0/unpublish`, async ({ request }) => {
            bodies.push(await request.json());
            return HttpResponse.json({});
          }),
        ],
      ),
    });
    await openMenu(user, "1.1.0");
    await user.click(
      screen.getByRole("menuitem", { name: "Unpublish version" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Unpublish 1.1.0?",
    });
    await waitFor(() => {
      expect(within(dialog).queryByText("Optional")).toBeNull();
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Unpublish 1.1.0" }),
    );
    expect(await within(dialog).findByText("Enter a reason.")).toBeVisible();
    expect(bodies).toEqual([]);
    await user.type(within(dialog).getByLabelText("Reason"), "  Superseded  ");
    expect(within(dialog).queryByText("Enter a reason.")).toBeNull();
    await user.click(
      within(dialog).getByRole("button", { name: "Unpublish 1.1.0" }),
    );
    expect(await screen.findByText("1.1.0 is unpublished.")).toBeVisible();
    expect(bodies).toEqual([{ reason: "Superseded" }]);
  });

  it("sends the deprecate reason with the sunset date, and shows the server's refusal on the field", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [preview, latest],
        [
          http.post(`${API}/versions/1.1.0/deprecate`, async ({ request }) => {
            const body = (await request.json()) as { reason?: string };
            bodies.push(body);
            return body.reason === undefined
              ? problem(422, "lifecycle-reason-required", {
                  detail:
                    "A reason is required to unpublish or deprecate a version.",
                })
              : HttpResponse.json(
                  version("1.1.0", {
                    status: "preview",
                    deprecated: true,
                    sunset_date: SUNSET,
                  }),
                );
          }),
        ],
      ),
    });
    await openMenu(user, "1.1.0");
    await user.click(
      screen.getByRole("menuitem", { name: "Deprecate version" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Deprecate 1.1.0?",
    });
    fireEvent.change(within(dialog).getByLabelText("Sunset date (UTC)"), {
      target: { value: SUNSET },
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Deprecate 1.1.0" }),
    );
    expect(await within(dialog).findByText("Enter a reason.")).toBeVisible();
    await user.type(within(dialog).getByLabelText(/^Reason/), "Use 1.2.0");
    await user.click(
      within(dialog).getByRole("button", { name: "Deprecate 1.1.0" }),
    );
    expect(
      await screen.findByText(`1.1.0 is deprecated; sunset ${SUNSET}.`),
    ).toBeVisible();
    expect(bodies).toEqual([
      { sunset_date: SUNSET },
      { sunset_date: SUNSET, reason: "Use 1.2.0" },
    ]);
  });

  it("promotes without naming a Latest when there is none, and keeps the dialog open on failure", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: ALL,
      handlers: versionsHandlers(
        [preview],
        [
          http.post(`${API}/versions/1.1.0/promote`, () =>
            problem(409, "promote-refused"),
          ),
        ],
      ),
    });
    await openMenu(user, "1.1.0");
    await user.click(
      screen.getByRole("menuitem", { name: "Promote to Latest" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Make 1.1.0 the Latest?",
    });
    expect(
      within(dialog).getByText(
        'Consumers fetching "latest" will get 1.1.0. Promoting a deprecated version clears its deprecation.',
      ),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Promote 1.1.0" }),
    );
    expect(
      await within(dialog).findByText("promote-refused detail"),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Keep 1.1.0" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });
});

// ---------------------------------------------------------------- One version

const REPORT = {
  version_id: "v-1.0.0",
  contract_id: "c-1",
  semver: "1.0.0",
  content_sha256: "sha",
  dsl_version: "cel-2",
  engine_version: "engine-7",
  schema_sha: "schema",
  run_id: "run-42",
  validated_at: "2026-11-02T10:00:00Z",
  pins: [
    {
      rule_set_id: "rs-1",
      name: "acme-baseline",
      scope: "organization",
      version: 2,
      source: "attached",
      mode: "warn",
      deprecated_version: true,
    },
    { rule_set_id: "rs-2", scope: "platform", version: 1, source: "mandatory" },
    { name: "native", scope: "platform", version: true },
    "not a record",
  ],
  report: {
    status: "completed",
    structural: { valid: true, error_count: 0, errors: [] },
    findings: [
      {
        rule_set: { id: "rs-1", name: "acme-baseline", version: 2 },
        source: "rule",
        mode: "warn",
        rule_key: "has-version",
        severity: "warning",
        outcome: "fail",
        message: "Version is odd.",
        location: { pointer: "/version" },
        blocking: false,
      },
    ],
    summary: { publishable: true, blocking_count: 0 },
  },
};

function versionPageHandlers(
  meta: Record<string, unknown>,
  extra: RequestHandler[] = [],
) {
  return [
    contractHandler,
    ...extra,
    http.get(`${API}/versions/1.0.0/metadata`, () => HttpResponse.json(meta)),
    http.get(`${API}/versions/1.0.0/validation-report`, () =>
      HttpResponse.json(REPORT),
    ),
    http.get(`${API}/versions/1.0.0`, ({ request }) => {
      const accept = request.headers.get("Accept") ?? "";
      return accept.includes("json")
        ? new HttpResponse('{"version": "1.0.0"}', {
            headers: { "Content-Type": "application/json" },
          })
        : new HttpResponse(DOC, {
            headers: { "Content-Type": "application/yaml" },
          });
    }),
  ];
}

describe("version page", () => {
  it("shows the document as stored or converted, and downloads it", async () => {
    const user = userEvent.setup();
    const saved = captureDownloads();
    renderApp("/contracts/c-1/versions/1.0.0", {
      folders,
      permissions: ALL,
      handlers: versionPageHandlers({
        ...latest,
        deprecated: true,
        deprecated_at: "2026-11-05T00:00:00Z",
        sunset_date: null,
      }),
    });
    expect(
      await screen.findByRole("region", { name: "Version 1.0.0" }),
    ).toBeVisible();
    expect(screen.getByText(/sunset on a date not set\./)).toBeVisible();
    const editor = await screen.findByRole("textbox", {
      name: "Version 1.0.0 document",
    });
    expect(editor).toHaveValue(DOC);
    expect(editor).toHaveAttribute("readonly");
    const stored = screen.getByRole("button", { name: "As stored" });
    expect(stored).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByRole("button", { name: "JSON" }));
    await waitFor(() => {
      expect(
        screen.getByRole("textbox", { name: "Version 1.0.0 document" }),
      ).toHaveValue('{"version": "1.0.0"}');
    });
    expect(screen.getByRole("button", { name: "JSON" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await user.click(screen.getByRole("button", { name: "Download YAML" }));
    await waitFor(() => {
      expect(saved.map((entry) => entry.name)).toEqual([
        "urn-acme-ledger-orders-1.0.0.yaml",
      ]);
    });
    await user.click(screen.getByRole("button", { name: "Download JSON" }));
    await waitFor(() => {
      expect(saved[1]?.name).toBe("urn-acme-ledger-orders-1.0.0.json");
    });
  });

  it("shows a document or download failure as a problem", async () => {
    const user = userEvent.setup();
    captureDownloads();
    let docCalls = 0;
    renderApp("/contracts/c-1/versions/1.0.0", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/versions/1.0.0/metadata`, () =>
          HttpResponse.json(latest),
        ),
        http.get(`${API}/versions/1.0.0`, () => {
          docCalls += 1;
          return problem(500, "document-broken");
        }),
      ],
    });
    expect(await screen.findByText("document-broken detail")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    await waitFor(() => {
      expect(docCalls).toBe(2);
    });
    await user.click(screen.getByRole("button", { name: "Download YAML" }));
    await waitFor(() => {
      expect(screen.getAllByText("document-broken detail")).toHaveLength(2);
    });
  });

  it("shows the stored gate report with its pins and locatable findings", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions/1.0.0", {
      folders,
      permissions: ALL,
      handlers: versionPageHandlers(latest),
    });
    await user.click(
      await screen.findByRole("tab", { name: "Validation report" }),
    );
    expect(await screen.findByText("Passed the gate")).toBeVisible();
    expect(screen.getByText("run-42")).toBeVisible();
    expect(screen.getByText("engine-7")).toBeVisible();
    expect(screen.getByRole("button", { name: "Copy run ID" })).toBeVisible();
    const baseline = screen.getByRole("link", { name: "acme-baseline" });
    expect(baseline).toHaveAttribute("href", "/rule-sets/rs-1");
    const baselineRow = baseline.closest("tr") as HTMLElement;
    expect(within(baselineRow).getByText("Deprecated")).toBeVisible();
    expect(within(baselineRow).getByText("warn")).toBeVisible();
    expect(within(baselineRow).getByText("attached")).toBeVisible();
    // A pin without a name links by its id; one without an id is plain text.
    const unnamed = screen.getByRole("link", { name: "rs-2" });
    expect(
      within(unnamed.closest("tr") as HTMLElement).getByText("enforce"),
    ).toBeVisible();
    expect(screen.getByText("native")).toBeVisible();

    const findings = screen.getByRole("region", {
      name: "Findings at publish",
    });
    expect(within(findings).getByText("Warnings (1)")).toBeVisible();
    expect(within(findings).getByText("acme-baseline v2")).toBeVisible();
    expect(within(findings).getByText("warn mode")).toBeVisible();
    await user.click(
      await within(findings).findByRole("button", { name: "Go to line 4" }),
    );
    const editor = screen.getAllByRole("textbox", {
      name: "Version 1.0.0 document",
    });
    const reportEditor = editor[editor.length - 1] as HTMLTextAreaElement;
    expect(reportEditor).toHaveFocus();
    expect(reportEditor.selectionStart).toBe(DOC.indexOf("version: 1.0.0"));
  });

  it("marks a report that was not publishable", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions/1.0.0", {
      folders,
      permissions: ALL,
      handlers: versionPageHandlers(latest, [
        http.get(`${API}/versions/1.0.0/validation-report`, () =>
          HttpResponse.json({
            ...REPORT,
            pins: [],
            report: { summary: { publishable: false } },
          }),
        ),
      ]),
    });
    await user.click(
      await screen.findByRole("tab", { name: "Validation report" }),
    );
    expect(await screen.findByText("Not publishable")).toBeVisible();
    expect(screen.getByText("No findings. Every check passed.")).toBeVisible();
  });

  it("explains a version published before reports were stored", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions/1.0.0", {
      folders,
      permissions: ALL,
      handlers: versionPageHandlers(latest, [
        http.get(`${API}/versions/1.0.0/validation-report`, () =>
          problem(404, "validation-report-not-recorded"),
        ),
      ]),
    });
    await user.click(
      await screen.findByRole("tab", { name: "Validation report" }),
    );
    expect(
      await screen.findByText(
        "No report recorded (published before the gate stored reports).",
      ),
    ).toBeVisible();
  });

  it("shows any other report failure with a retry", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/contracts/c-1/versions/1.0.0", {
      folders,
      permissions: ALL,
      handlers: versionPageHandlers(latest, [
        http.get(`${API}/versions/1.0.0/validation-report`, () => {
          calls += 1;
          return problem(500, "report-broken");
        }),
      ]),
    });
    await user.click(
      await screen.findByRole("tab", { name: "Validation report" }),
    );
    expect(await screen.findByText("report-broken detail")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    await waitFor(() => {
      expect(calls).toBe(2);
    });
  });

  it("lists the version metadata", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions/1.0.0", {
      folders,
      permissions: ALL,
      handlers: versionPageHandlers({
        ...latest,
        published_at: null,
        schema_sha: null,
      }),
    });
    await user.click(await screen.findByRole("tab", { name: "Metadata" }));
    const panel = await screen.findByRole("tabpanel");
    expect(within(panel).getByText("v-1.0.0")).toBeVisible();
    // Neither the publish time nor the schema was recorded.
    expect(within(panel).getAllByText("Not recorded")).toHaveLength(2);
    expect(within(panel).getByText("No")).toBeVisible();
    expect(
      within(panel).getByText(
        `${window.location.origin}/api/v1/contracts/contracts/c-1/versions/1.0.0`,
      ),
    ).toBeVisible();
    expect(within(panel).getByText('"e-1.0.0"')).toBeVisible();
    expect(within(panel).getByText("320 bytes")).toBeVisible();
    expect(
      within(panel).getByRole("button", { name: "Copy fetch URL" }),
    ).toBeVisible();
  });

  it("lists deprecation and the schema in the metadata", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions/1.0.0", {
      folders,
      permissions: ALL,
      handlers: versionPageHandlers({
        ...latest,
        deprecated: true,
        deprecated_at: "2026-11-05T00:00:00Z",
        sunset_date: "2027-02-01",
        schema_sha: "schema-abc",
      }),
    });
    expect(await screen.findByText(/sunset on 2027-02-01\./)).toBeVisible();
    await user.click(await screen.findByRole("tab", { name: "Metadata" }));
    const panel = await screen.findByRole("tabpanel");
    expect(
      within(panel).getByText(/^Yes, since .*; sunset 2027-02-01$/),
    ).toBeVisible();
    expect(within(panel).getByText("schema-abc")).toBeVisible();
  });

  it("shows Not found for an unknown version and a problem otherwise", async () => {
    renderApp("/contracts/c-1/versions/9.9.9", {
      folders,
      permissions: ALL,
      handlers: [
        contractHandler,
        http.get(`${API}/versions/9.9.9/metadata`, () =>
          problem(404, "not-found"),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "Not found" }),
    ).toBeVisible();
  });

  it("retries a failed metadata read", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/contracts/c-1/versions/1.0.0", {
      folders,
      permissions: ALL,
      handlers: versionPageHandlers(latest, [
        http.get(`${API}/versions/1.0.0/metadata`, () => {
          calls += 1;
          return calls === 1
            ? problem(500, "metadata-broken")
            : HttpResponse.json(latest);
        }),
      ]),
    });
    expect(await screen.findByText("metadata-broken detail")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("region", { name: "Version 1.0.0" }),
    ).toBeVisible();
  });

  it("runs a lifecycle action from the version page and shows its notice", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/versions/1.0.0", {
      folders,
      permissions: ALL,
      handlers: versionPageHandlers(latest, [
        http.post(`${API}/versions/1.0.0/unpublish`, () =>
          HttpResponse.json({}),
        ),
      ]),
    });
    await user.click(
      await screen.findByRole("button", { name: "Actions for 1.0.0" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Unpublish version" }),
    );
    const dialog = await screen.findByRole("alertdialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Unpublish 1.0.0" }),
    );
    expect(await screen.findByText("1.0.0 is unpublished.")).toBeVisible();
  });
});
