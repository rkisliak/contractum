// Contract screens through the real router: the global list, the overview with a blocked delete,
// the draft editor (save with If-Match, the 412 conflict, the publish gate result), the versions
// tab with a lifecycle action, and creating a contract from the starter.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder } from "@/test/renderApp";

afterEach(() => {
  tokenStore.clear();
});

const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const payments = makeFolder({
  id: "payments-id",
  parent_id: "acme-id",
  kind: "project",
  slug: "payments",
  display_name: "Payments",
  path: "acme/payments",
});
const ledger = makeFolder({
  id: "ledger-id",
  parent_id: "payments-id",
  kind: "team",
  slug: "ledger",
  display_name: "Ledger",
  path: "acme/payments/ledger",
});
const folders = [rootFolder, acme, payments, ledger];

const CONTRACT_PERMS = [
  "contract:read",
  "contract:update",
  "contract:publish",
  "contract:delete",
  "contract:restore",
  "contract:create",
];

const contract = {
  id: "c-1",
  folder_id: "ledger-id",
  organization_id: "acme-id",
  odcs_id: "urn:acme:ledger:orders",
  notation: "odcs",
  name: "Orders",
  status: "active",
  api_version: "v3.1.0",
  domain: "payments",
  tags: ["demo"],
  media_type: "application/yaml",
  content_sha256: null,
  size_bytes: null,
  content_redacted: false,
  template: null,
  origin: null,
  version: { number: 2, state: "draft", deprecated: false },
  latest_version: { version_id: "v-1", semver: "1.0.0" },
  created_by: "local:admin",
  created_at: "2026-11-01T10:00:00Z",
  updated_by: "local:admin",
  updated_at: "2026-11-02T10:00:00Z",
};

function version(semver: string, status: string) {
  return {
    version_id: `v-${semver}`,
    contract_id: "c-1",
    semver,
    status,
    lifecycle: "published",
    deprecated: false,
    deprecated_at: null,
    sunset_date: null,
    content_sha256: "abc",
    etag: '"abc"',
    size_bytes: 320,
    media_type: "application/yaml",
    odcs_api_version: "v3.1.0",
    published_at: "2026-11-02T10:00:00Z",
    published_by: "local:admin",
    fetch_url: `/api/v1/contracts/contracts/c-1/versions/${semver}`,
    validation_run_id: null,
    pinned_rule_sets: null,
    engine_version: null,
    schema_sha: null,
  };
}

function draft(revision: number) {
  return {
    id: "d-1",
    contract_id: "c-1",
    revision,
    etag: `"r${String(revision)}"`,
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
  };
}

// `status: active`: a released version with another status cannot be published from the dialog.
const DOC =
  "apiVersion: v3.1.0\nkind: DataContract\nid: urn:acme:ledger:orders\nversion: 1.1.0\nstatus: active\n";

function problem(
  status: number,
  slug: string,
  extra: Record<string, unknown> = {},
) {
  return HttpResponse.json(
    {
      type: `urn:dc:problem:${slug}`,
      title: "Refused",
      status,
      detail: `${slug} detail`,
      ...extra,
    },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

const base = [
  http.get("*/api/v1/contracts/contracts/c-1", () =>
    HttpResponse.json(contract),
  ),
  http.get("*/api/v1/contracts/contracts/c-1/versions", () =>
    HttpResponse.json({
      items: [version("1.1.0", "preview"), version("1.0.0", "latest")],
    }),
  ),
  http.get("*/api/v1/contracts/contracts/c-1/drafts", () =>
    HttpResponse.json({ items: [draft(1)] }),
  ),
  http.get("*/api/v1/contracts/contracts/c-1/drafts/d-1", () =>
    HttpResponse.json(draft(1)),
  ),
  http.get(
    "*/api/v1/contracts/contracts/c-1/drafts/d-1/document",
    () =>
      new HttpResponse(DOC, {
        headers: { "Content-Type": "application/yaml", ETag: '"r1"' },
      }),
  ),
];

describe("contract list", () => {
  it("lists visible contracts with folder and latest version", async () => {
    renderApp("/contracts", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({ items: [contract], next_cursor: null }),
        ),
      ],
    });
    expect(await screen.findByRole("link", { name: "Orders" })).toBeVisible();
    expect(
      screen.getByRole("link", { name: "acme/payments/ledger" }),
    ).toBeVisible();
    expect(screen.getByText("1.0.0")).toBeVisible();
    expect(screen.getByRole("button", { name: "New contract" })).toBeVisible();
  });
});

describe("contract list search", () => {
  it("drops a pending debounced search once a result is opened", async () => {
    const { router } = renderApp("/contracts", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        ...base,
        http.get("*/api/v1/contracts/contracts", () =>
          HttpResponse.json({ items: [contract], next_cursor: null }),
        ),
      ],
    });
    const link = await screen.findByRole("link", { name: "Orders" });
    // Type, then open the result inside the 300 ms debounce window. Both are single synchronous
    // events: per-keystroke typing on a loaded machine can outlast the window and re-run the
    // search, which unmounts the link before the click.
    fireEvent.change(
      within(screen.getByRole("main")).getByLabelText("Search contracts"),
      { target: { value: "Ord" } },
    );
    fireEvent.click(link);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/c-1");
    });
    await new Promise((resolve) => setTimeout(resolve, 450));
    expect(router.state.location.pathname).toBe("/contracts/c-1");
  });
});

describe("contract overview", () => {
  it("explains a delete blocked by a reference with the referrer link", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        ...base,
        http.delete("*/api/v1/contracts/contracts/c-1", () =>
          problem(409, "contract-referenced", {
            referrers: [{ contract_id: "c-9", odcs_id: "urn:acme:other" }],
            hidden_referrer_count: 2,
          }),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Orders" }),
    ).toBeVisible();
    await user.click(
      await screen.findByRole("button", { name: "Delete contract" }),
    );
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByRole("textbox"), "Orders");
    await user.click(
      within(dialog).getByRole("button", { name: "Delete Orders" }),
    );
    expect(
      await screen.findByText("This contract is still referenced"),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "urn:acme:other" })).toBeVisible();
    expect(screen.getByText("and 2 more you cannot see")).toBeVisible();
  });
});

describe("contract pages for a published-only reader (consumer)", () => {
  const CONSUMER = ["contract:read-published", "contract:view", "card:create"];

  it("offers only published reading: no drafts, rule sets or subscribers", async () => {
    renderApp("/contracts/c-1", {
      folders,
      permissions: CONSUMER,
      handlers: [...base],
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Orders" }),
    ).toBeVisible();
    const sections = screen.getByRole("navigation", {
      name: "Contract sections",
    });
    await waitFor(() => {
      expect(
        within(sections).queryByRole("link", { name: "Drafts" }),
      ).toBeNull();
    });
    expect(
      within(sections)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Overview", "Document", "Versions", "Diff"]);
    expect(screen.queryByRole("link", { name: "Open drafts" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Delete contract" }),
    ).toBeNull();
  });

  it("shows the Latest published document instead of the current one", async () => {
    let currentRequested = false;
    renderApp("/contracts/c-1/document", {
      folders,
      permissions: CONSUMER,
      handlers: [
        ...base,
        http.get("*/api/v1/contracts/contracts/c-1/document", () => {
          currentRequested = true;
          return HttpResponse.json({}, { status: 404 });
        }),
        http.get(
          "*/api/v1/contracts/contracts/c-1/versions/latest",
          () =>
            new HttpResponse(DOC, {
              headers: { "Content-Type": "application/yaml" },
            }),
        ),
      ],
    });
    expect(await screen.findByLabelText("Contract document")).toHaveValue(DOC);
    expect(currentRequested).toBe(false);
    expect(screen.queryByText("Request ID")).toBeNull();
  });
});

describe("draft editor", () => {
  it("saves with If-Match and reports success", async () => {
    const user = userEvent.setup();
    const seen: (string | null)[] = [];
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        ...base,
        http.put(
          "*/api/v1/contracts/contracts/c-1/drafts/d-1",
          ({ request }) => {
            seen.push(request.headers.get("If-Match"));
            return HttpResponse.json({
              draft: draft(2),
              structural_validation: {
                valid: true,
                error_count: 0,
                errors: [],
              },
              validation: { status: "unavailable" },
            });
          },
        ),
      ],
    });
    const editor = await screen.findByRole("textbox", {
      name: "Draft 1.1.0 document",
    });
    await user.type(editor, "status: active");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText("Draft saved.")).toBeVisible();
    expect(seen).toEqual(['"r1"']);
    expect(screen.getByText("Revision 2")).toBeVisible();
  });

  it("opens the conflict dialog on 412 and keeps my edits with the new ETag", async () => {
    const user = userEvent.setup();
    const seen: (string | null)[] = [];
    let current = 1;
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        http.get("*/api/v1/contracts/contracts/c-1/drafts/d-1", () =>
          HttpResponse.json(draft(current)),
        ),
        ...base,
        http.put(
          "*/api/v1/contracts/contracts/c-1/drafts/d-1",
          ({ request }) => {
            const ifMatch = request.headers.get("If-Match");
            seen.push(ifMatch);
            if (ifMatch === '"r1"') {
              current = 3;
              return problem(412, "precondition-failed");
            }
            return HttpResponse.json({
              draft: draft(4),
              structural_validation: {
                valid: true,
                error_count: 0,
                errors: [],
              },
              validation: { status: "unavailable" },
            });
          },
        ),
      ],
    });
    const editor = await screen.findByRole("textbox", {
      name: "Draft 1.1.0 document",
    });
    await user.type(editor, "x");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    const dialog = await screen.findByRole("dialog", {
      name: "This draft changed on the server",
    });
    expect(within(dialog).getByText(/Someone saved revision 3/)).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Keep my edits" }),
    );
    await waitFor(() => {
      expect(seen).toEqual(['"r1"', '"r3"']);
    });
    expect(
      await screen.findByText("Draft saved over the newer server copy."),
    ).toBeVisible();
  });

  it("shows the publish gate result with the semver callout", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        ...base,
        http.get("*/api/v1/contracts/contracts/c-1/diff", () =>
          HttpResponse.json({
            from: {
              ref: "latest",
              kind: "version",
              version_id: "v-1",
              semver: "1.0.0",
              content_sha256: "a",
            },
            to: {
              ref: "d-1",
              kind: "draft",
              version_id: "d-1",
              semver: null,
              content_sha256: "b",
            },
            classifier_version: "odcs-compat-1",
            required_bump: "major",
            counts: { breaking: 1, warning: 0, info: 0 },
            changes: [],
            changes_total: 1,
            truncated: false,
            next_cursor: null,
          }),
        ),
        http.post("*/api/v1/contracts/contracts/c-1/drafts/d-1/publish", () =>
          problem(422, "contract-validation-failed", {
            run_id: "run-77",
            report: {
              status: "completed",
              structural: { valid: true, error_count: 0, errors: [] },
              findings: [
                {
                  rule_set: { id: null, name: null, version: null },
                  source: "native",
                  mode: "enforce",
                  rule_key: "semver-breaking-change",
                  severity: "error",
                  outcome: "fail",
                  message:
                    "A breaking change needs a new major version. The smallest passing version is 2.0.0.",
                  location: { pointer: "/version", path: "$.version" },
                  blocking: true,
                },
              ],
              summary: { publishable: false, blocking_count: 1 },
            },
          }),
        ),
      ],
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Publish 1.1.0?",
    });
    expect(
      await within(dialog).findByText(
        /This change needs a major bump; you declared minor/,
      ),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Publish 1.1.0" }),
    );
    expect(
      await screen.findByText("Publish blocked: 1 blocking finding"),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Breaking changes need a new major version. Declared 1.1.0; required at least 2.0.0.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Go to line 4" })).toBeVisible();
    expect(
      screen.getByRole("link", { name: "View breaking changes" }),
    ).toBeVisible();
  });
});

describe("versions tab", () => {
  it("shows status badges and promotes a Preview after confirmation", async () => {
    const user = userEvent.setup();
    let promoted = false;
    renderApp("/contracts/c-1/versions", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        ...base,
        http.post(
          "*/api/v1/contracts/contracts/c-1/versions/1.1.0/promote",
          () => {
            promoted = true;
            return HttpResponse.json(version("1.1.0", "latest"));
          },
        ),
      ],
    });
    expect(await screen.findByText("Preview")).toBeVisible();
    expect(screen.getByText("Latest")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Actions for 1.1.0" }));
    await user.click(
      await screen.findByRole("menuitem", { name: "Promote to Latest" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Make 1.1.0 the Latest?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Promote 1.1.0" }),
    );
    expect(await screen.findByText("1.1.0 is now the Latest.")).toBeVisible();
    expect(promoted).toBe(true);
  });
});

describe("new contract", () => {
  it("creates from the ODCS starter and opens the draft editor", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    const { router } = renderApp("/contracts/new?folder=ledger-id", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        ...base,
        http.post(
          "*/api/v1/contracts/folders/ledger-id/contracts/starter",
          async ({ request }) => {
            body = await request.json();
            return HttpResponse.json(
              {
                contract,
                version: { number: 1, state: "draft", deprecated: false },
                draft: { id: "d-1", revision: 1, etag: '"r1"' },
                structural_validation: {
                  valid: true,
                  error_count: 0,
                  errors: [],
                },
                validation: { status: "unavailable" },
              },
              { status: 201 },
            );
          },
        ),
      ],
    });
    await user.type(
      await screen.findByRole("textbox", { name: "Name" }),
      "Orders",
    );
    expect(screen.getByRole("textbox", { name: /ODCS id/ })).toHaveValue(
      "urn:acme:payments:ledger:orders",
    );
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/c-1/drafts/d-1");
    });
    expect(body).toEqual({
      odcs_id: "urn:acme:payments:ledger:orders",
      name: "Orders",
      notation: "odcs",
    });
  });
});

describe("the name rule and the ODCS id bound on new contract", () => {
  it("shows the message for an emoji in the starter name and sends nothing", async () => {
    const user = userEvent.setup();
    let posted = false;
    renderApp("/contracts/new?folder=ledger-id", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        ...base,
        http.post(
          "*/api/v1/contracts/folders/ledger-id/contracts/starter",
          () => {
            posted = true;
            return HttpResponse.json({}, { status: 201 });
          },
        ),
      ],
    });
    await user.type(
      await screen.findByRole("textbox", { name: "Name" }),
      "Orders 😀",
    );
    expect(
      screen.getByText(
        "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    expect(posted).toBe(false);
  });
});

describe("the ODCS id bound on new contract", () => {
  it("stops the ODCS id input at 255 characters", async () => {
    renderApp("/contracts/new?folder=ledger-id", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [...base],
    });
    expect(
      await screen.findByRole("textbox", { name: /ODCS id/ }),
    ).toHaveAttribute("maxlength", "255");
  });
});

describe("new contract in any folder", () => {
  const created = {
    contract,
    version: { number: 1, state: "draft", deprecated: false },
    draft: { id: "d-1", revision: 1, etag: '"r1"' },
    structural_validation: { valid: true, error_count: 0, errors: [] },
    validation: { status: "unavailable" },
  };

  it("lists every non-archived folder, Root first, and creates at an organization", async () => {
    const user = userEvent.setup();
    let posted = false;
    const archivedTeam = makeFolder({
      id: "old-id",
      parent_id: "payments-id",
      kind: "team",
      slug: "old",
      display_name: "Old",
      path: "acme/payments/old",
      archived: true,
      archived_at: "2026-11-05T10:00:00Z",
    });
    const { router } = renderApp("/contracts/new?folder=acme-id", {
      folders: [...folders, archivedTeam],
      permissions: CONTRACT_PERMS,
      handlers: [
        ...base,
        http.post(
          "*/api/v1/contracts/folders/acme-id/contracts/starter",
          () => {
            posted = true;
            return HttpResponse.json(created, { status: 201 });
          },
        ),
      ],
    });
    const select = await screen.findByRole("combobox", { name: "Folder" });
    await waitFor(() => {
      expect(select).toHaveValue("acme-id");
    });
    const labels = within(select)
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(labels).toEqual([
      "Choose a folder",
      "Root",
      "acme",
      "acme/payments",
      "acme/payments/ledger",
    ]);
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Shared");
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/c-1/drafts/d-1");
    });
    expect(posted).toBe(true);
  });

  it("tells a caller without contract:create at the folder and refuses to submit", async () => {
    const user = userEvent.setup();
    let posted = false;
    renderApp("/contracts/new?folder=payments-id", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        http.get("*/api/v1/identity/me/capabilities", ({ request }) => {
          const folderId = new URL(request.url).searchParams.get("folder_id");
          return HttpResponse.json({
            folder_id: folderId,
            roles: ["team_reader"],
            permissions:
              folderId === "payments-id" ? ["contract:read"] : CONTRACT_PERMS,
            inheritance_blocked: false,
            grantable_roles: [],
          });
        }),
        ...base,
        http.post("*/api/v1/contracts/folders/payments-id/*", () => {
          posted = true;
          return HttpResponse.json(created, { status: 201 });
        }),
      ],
    });
    expect(
      await screen.findByText("You cannot create contracts in this folder."),
    ).toBeVisible();
    await user.type(await screen.findByRole("textbox", { name: "Name" }), "X");
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You cannot create contracts in this folder.",
    );
    expect(posted).toBe(false);
  });
});

describe("new contract from a template", () => {
  it("offers templates from every page of the available list", async () => {
    const cursors: (string | null)[] = [];
    const tpl = (id: string, name: string) => ({
      template_id: id,
      name,
      folder_id: "root-id",
      latest_version_no: 1,
      version_nos: [1],
    });
    renderApp("/contracts/new?folder=ledger-id&template=t-late", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: [
        ...base,
        http.get(
          "*/api/v1/contracts/folders/ledger-id/available-templates",
          ({ request }) => {
            const url = new URL(request.url);
            const cursor = url.searchParams.get("cursor");
            cursors.push(cursor);
            expect(url.searchParams.get("limit")).toBe("100");
            return HttpResponse.json(
              cursor === null
                ? { items: [tpl("t-1", "First table")], next_cursor: "p2" }
                : { items: [tpl("t-late", "Late table")], next_cursor: null },
            );
          },
        ),
        http.get(
          "*/api/v1/contracts/folders/ledger-id/available-templates/:id/versions/:no/document",
          () =>
            new HttpResponse("kind: DataContract\n", {
              headers: { "Content-Type": "application/yaml" },
            }),
        ),
      ],
    });
    const select = await screen.findByRole("combobox", { name: "Template" });
    await waitFor(() => {
      expect(select).toHaveValue("t-late");
    });
    expect(
      within(select).getByRole("option", { name: "First table (latest v1)" }),
    ).toBeInTheDocument();
    expect(cursors).toEqual([null, "p2"]);
  });
});
