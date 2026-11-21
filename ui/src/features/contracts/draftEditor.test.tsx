// The draft editor beyond the happy save: read-only modes, validate (findings, stale results,
// go to line), save failures, the conflict dialog's reload, every publish outcome (success,
// semver refusal, a 412, a plain failure), discard, the unsaved-changes guard and downloads.
// Also the pure helpers of the contract api, the publish dialog and the findings panel.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createApiClient } from "@/api/client";
import type { Problem } from "@/api/problem";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse, server } from "@/test/msw";
import {
  makeFolder,
  renderApp,
  rootFolder,
  testConfig,
} from "@/test/renderApp";
import {
  absoluteUrl,
  fetchContractDocument,
  fetchDraftDocument,
  fetchVersionDocument,
  fileNameFor,
  formatOf,
  mediaTypeOf,
  shortPrincipal,
} from "./api";
import { breakingCopy, idMismatchOf, semverConflictCopy } from "./DraftEditor";
import { markersFor } from "./FindingsPanel";
import { declaredBump, documentStatus } from "./PublishDialog";
import type { UnifiedFinding } from "./report";
import { curlExample, todayUtc } from "./VersionActions";

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
];

const contract = {
  id: "c-1",
  folder_id: "ledger-id",
  organization_id: "ledger-id",
  odcs_id: "urn:acme:ledger:orders",
  notation: "odcs",
  name: "Orders",
  status: "active",
  api_version: "v3.1.0",
  domain: null,
  tags: [],
  media_type: "application/yaml",
  content_sha256: null,
  size_bytes: null,
  content_redacted: false,
  template: null,
  origin: null,
  version: { number: 2, state: "draft" },
  latest_version: { version_id: "v-1.0.0", semver: "1.0.0" },
  created_by: "local:admin",
  created_at: "2026-11-01T10:00:00Z",
  updated_by: "local:admin",
  updated_at: "2026-11-02T10:00:00Z",
};

function draft(revision: number, overrides: Record<string, unknown> = {}) {
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
    ...overrides,
  };
}

const DOC =
  "apiVersion: v3.1.0\nkind: DataContract\nid: urn:acme:ledger:orders\nversion: 1.1.0\n";

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

const REPORT = {
  status: "completed",
  structural: {
    valid: false,
    error_count: 1,
    errors: [
      { keyword: "required", message: "kind is required", pointer: "/kind" },
      "not a record",
    ],
  },
  findings: [
    {
      rule_set: { id: "rs-1", name: "acme", version: 3 },
      source: "rule",
      mode: "warn",
      rule_key: "has-domain",
      severity: "warning",
      outcome: "fail",
      message: "Set a domain.",
      location: { pointer: "/domain" },
      blocking: false,
    },
    {
      rule_set: { id: null, name: null, version: null },
      source: "native",
      mode: "enforce",
      rule_key: "info-only",
      severity: "info",
      outcome: "pass",
      message: "Looks fine.",
      location: { pointer: "" },
      blocking: false,
    },
    {
      rule_set: {},
      source: "rule",
      severity: "error",
      outcome: "error",
      message: "Rule crashed.",
      location: {},
      blocking: true,
    },
    {
      rule_set: { name: "acme" },
      source: "rule",
      severity: "error",
      outcome: "fail",
      message: "Bad pointer.",
      location: { pointer: "no-slash" },
      blocking: true,
    },
  ],
  summary: { publishable: false, blocking_count: 3 },
};

function saveResponse(revision: number, validation: unknown) {
  return {
    draft: draft(revision),
    structural_validation: { valid: true, error_count: 0, errors: [] },
    validation,
  };
}

function base(
  extra: RequestHandler[] = [],
  {
    meta = draft(1),
    contractBody = contract,
    docText = DOC,
  }: {
    meta?: ReturnType<typeof draft>;
    contractBody?: Record<string, unknown>;
    docText?: string;
  } = {},
): RequestHandler[] {
  return [
    ...extra,
    http.get(API, () => HttpResponse.json(contractBody)),
    http.get(`${API}/drafts/d-1`, () => HttpResponse.json(meta)),
    http.get(
      `${API}/drafts/d-1/document`,
      () =>
        new HttpResponse(docText, {
          headers: { "Content-Type": "application/yaml" },
        }),
    ),
    http.get(`${API}/drafts`, () => HttpResponse.json({ items: [meta] })),
    http.get(`${API}/versions`, () => HttpResponse.json({ items: [] })),
  ];
}

afterEach(() => {
  tokenStore.clear();
  vi.restoreAllMocks();
});

async function editorBox() {
  return screen.findByRole("textbox", { name: "Draft 1.1.0 document" });
}

describe("draft editor states", () => {
  it("shows Not found for an unknown draft", async () => {
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: [
        http.get(`${API}/drafts/d-1`, () => problem(404, "not-found")),
        ...base(),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "Not found" }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: /drafts/i })).toHaveAttribute(
      "href",
      "/contracts/c-1/drafts",
    );
  });

  it("shows a document failure with a retry", async () => {
    const user = userEvent.setup();
    let metaCalls = 0;
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: [
        http.get(`${API}/drafts/d-1`, () => {
          metaCalls += 1;
          return HttpResponse.json(draft(1));
        }),
        http.get(`${API}/drafts/d-1/document`, () =>
          problem(500, "document-broken"),
        ),
        ...base(),
      ],
    });
    expect(await screen.findByText("document-broken detail")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    await waitFor(() => {
      expect(metaCalls).toBe(2);
    });
  });

  it("is read-only without contract:update, with no save, discard or publish", async () => {
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ["contract:read"],
      handlers: base(),
    });
    expect(
      await screen.findByText("You can view this draft but not edit it."),
    ).toBeVisible();
    expect(await editorBox()).toHaveAttribute("readonly");
    expect(screen.queryByRole("button", { name: "Save draft" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Discard draft" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Publish" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Validate draft" }),
    ).toBeVisible();
    expect(screen.getByText("local:bob").parentElement).toHaveTextContent(
      "Last saved by local:bob",
    );
  });

  it("is read-only in an archived folder even with every right", async () => {
    renderApp("/contracts/c-1/drafts/d-1", {
      folders: [rootFolder, { ...ledger, archived: true }],
      permissions: ALL,
      handlers: base(),
    });
    expect(
      await screen.findByText("Archived folder: read-only."),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Publish" })).toBeNull();
  });

  it("offers no comparison when nothing is published yet", async () => {
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([], {
        contractBody: { ...contract, latest_version: null },
      }),
    });
    await editorBox();
    expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled();
    expect(
      screen.queryByRole("link", { name: "Compare with Latest" }),
    ).toBeNull();
  });
});

describe("draft editor saving and validating", () => {
  it("validates a clean buffer and lists findings by group with locations", async () => {
    const user = userEvent.setup();
    let validated = 0;
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.post(`${API}/drafts/d-1/validate`, () => {
          validated += 1;
          return HttpResponse.json({ report: REPORT });
        }),
      ]),
    });
    expect(
      await screen.findByRole("link", { name: "Compare with Latest" }),
    ).toHaveAttribute("href", "/contracts/c-1/diff?from=latest&to=d-1");
    await user.click(screen.getByRole("button", { name: "Validate draft" }));
    const panel = await screen.findByRole("region", {
      name: "Validation findings",
    });
    expect(validated).toBe(1);
    expect(within(panel).getByText("Blocking (3)")).toBeVisible();
    expect(within(panel).getByText("Warnings (1)")).toBeVisible();
    expect(within(panel).getByText("Info (1)")).toBeVisible();
    expect(within(panel).getByText("ODCS schema")).toBeVisible();
    expect(within(panel).getByText("required")).toBeVisible();
    expect(within(panel).getByText("Rule error")).toBeVisible();
    expect(within(panel).getByText("Platform check")).toBeVisible();
    expect(within(panel).getByText("acme v3")).toBeVisible();
    expect(within(panel).getByText("Rule set")).toBeVisible();
    expect(within(panel).getByText("/ (document)")).toBeVisible();
    // A missing member points at its nearest ancestor; a broken pointer has no location.
    expect(within(panel).getByText("(at parent) Set a domain.")).toBeVisible();
    expect(within(panel).getAllByText("location unavailable")).toHaveLength(2);

    await user.click(
      within(panel).getByRole("button", { name: "Go to line 2" }),
    );
    const editor = await editorBox();
    expect(editor).toHaveFocus();
    expect((editor as HTMLTextAreaElement).selectionStart).toBe(
      DOC.indexOf("kind:"),
    );

    // Editing makes the results stale (quick 261119-wk8): a warning names the revision, the old
    // list is collapsed behind a button, nothing is labelled "Blocking" and there are no locations.
    await user.type(editor, "x");
    expect(
      await within(panel).findByText(
        "These findings are for revision 1. Save and validate to check the current text.",
      ),
    ).toBeVisible();
    expect(
      within(panel).getByRole("button", { name: "Save and validate" }),
    ).toBeVisible();
    expect(within(panel).queryByText("Blocking (3)")).toBeNull();
    expect(within(panel).queryByText("Earlier errors (3)")).toBeNull();
    expect(
      within(panel).queryByRole("button", { name: /Go to line/ }),
    ).toBeNull();
    const toggle = within(panel).getByRole("button", {
      name: "Show previous findings",
    });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(within(panel).getByText("Earlier errors (3)")).toBeVisible();
    expect(within(panel).getByText("Earlier warnings (1)")).toBeVisible();
    expect(within(panel).queryByText("Blocking (3)")).toBeNull();
    expect(
      within(panel).queryByRole("button", { name: /Go to line/ }),
    ).toBeNull();
    await user.click(
      within(panel).getByRole("button", { name: "Hide previous findings" }),
    );
    expect(within(panel).queryByText("Earlier errors (3)")).toBeNull();
  });

  it("revalidates from the stale notice, which saves first", async () => {
    const user = userEvent.setup();
    let puts = 0;
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.put(`${API}/drafts/d-1`, () => {
          puts += 1;
          return HttpResponse.json(saveResponse(2, { status: "unavailable" }));
        }),
        http.post(`${API}/drafts/d-1/validate`, () =>
          HttpResponse.json({ report: REPORT }),
        ),
      ]),
    });
    await user.click(
      await screen.findByRole("button", { name: "Validate draft" }),
    );
    const panel = await screen.findByRole("region", {
      name: "Validation findings",
    });
    expect(
      within(panel).queryByText(/These findings are for revision/),
    ).toBeNull();
    await user.type(await editorBox(), "owner: someone");
    await user.click(
      await within(panel).findByRole("button", { name: "Save and validate" }),
    );
    await waitFor(() => {
      expect(puts).toBe(1);
    });
    await waitFor(() => {
      expect(
        within(
          screen.getByRole("region", { name: "Validation findings" }),
        ).queryByText(/These findings are for revision/),
      ).toBeNull();
    });
    expect(screen.getByText("Blocking (3)")).toBeVisible();
  });

  it("names both ids after an odcs-id-mismatch and puts the contract's id back without saving", async () => {
    const user = userEvent.setup();
    let puts = 0;
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.put(`${API}/drafts/d-1`, () => {
          puts += 1;
          return problem(422, "odcs-id-mismatch", {
            expected_odcs_id: "urn:acme:ledger:orders",
            found_odcs_id: "urn:acme:pasted:sample",
          });
        }),
      ]),
    });
    const editor = await editorBox();
    const extra = "owner: someone\n";
    const pasted = (DOC + extra).replace(
      "urn:acme:ledger:orders",
      "urn:acme:pasted:sample",
    );
    fireEvent.change(editor, { target: { value: pasted } });
    await user.click(screen.getByRole("button", { name: "Save draft" }));

    const alert = await screen.findByRole("alert");
    expect(
      within(alert).getByText(
        "Draft not saved: the document id does not match this contract",
      ),
    ).toBeVisible();
    expect(alert).toHaveTextContent(
      "This contract's id is urn:acme:ledger:orders. The document says urn:acme:pasted:sample.",
    );
    // Both ids read in the normal font, not monospace.
    expect(within(alert).getByText("urn:acme:ledger:orders").tagName).toBe(
      "SPAN",
    );
    expect(puts).toBe(1);

    await user.click(
      within(alert).getByRole("button", { name: "Put the contract's id back" }),
    );
    expect(editor).toHaveValue(DOC + extra);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(puts).toBe(1); // nothing is saved automatically
    expect(screen.getByText("Unsaved changes")).toBeVisible();
    expect(screen.getByRole("button", { name: "Save draft" })).toBeEnabled();
  });

  it("offers no id fix when the buffer cannot be edited safely", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.put(`${API}/drafts/d-1`, () =>
          problem(422, "odcs-id-mismatch", {
            expected_odcs_id: "urn:acme:ledger:orders",
            found_odcs_id: "urn:acme:pasted:sample",
          }),
        ),
      ]),
    });
    const editor = await editorBox();
    fireEvent.change(editor, { target: { value: "kind: DataContract\n" } });
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "This contract's id is urn:acme:ledger:orders.",
    );
    expect(
      within(alert).queryByRole("button", {
        name: "Put the contract's id back",
      }),
    ).toBeNull();
  });

  it("saves before validating a dirty buffer, and shows a validate failure", async () => {
    const user = userEvent.setup();
    const order: string[] = [];
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.put(`${API}/drafts/d-1`, async ({ request }) => {
          order.push(`put:${request.headers.get("Content-Type") ?? ""}`);
          order.push(await request.text());
          return HttpResponse.json(saveResponse(2, REPORT));
        }),
        http.post(`${API}/drafts/d-1/validate`, () => {
          order.push("validate");
          return problem(503, "validation-unavailable");
        }),
      ]),
    });
    const editor = await editorBox();
    await user.type(editor, "x");
    expect(screen.getByText("Unsaved changes")).toBeVisible();
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Save and validate" }));
    await waitFor(() => {
      expect(order).toEqual(["put:application/yaml", `${DOC}x`, "validate"]);
    });
    // The 503 shows the fixed unavailable wording, and the save's own report stays listed.
    expect(
      await screen.findByText("Service temporarily unavailable"),
    ).toBeVisible();
    expect(screen.getByText("Saved")).toBeVisible();
    expect(screen.getByText("Revision 2")).toBeVisible();
    expect(screen.getByText("kind is required")).toBeVisible();
  });

  it("stops before validating when the save fails, and saves with Ctrl+S", async () => {
    const user = userEvent.setup();
    let puts = 0;
    let validated = false;
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.put(`${API}/drafts/d-1`, () => {
          puts += 1;
          return puts === 1
            ? problem(422, "draft-invalid")
            : HttpResponse.json(saveResponse(2, { status: "unavailable" }));
        }),
        http.post(`${API}/drafts/d-1/validate`, () => {
          validated = true;
          return HttpResponse.json({ report: {} });
        }),
      ]),
    });
    const editor = await editorBox();
    await user.type(editor, "x");
    await user.click(screen.getByRole("button", { name: "Save and validate" }));
    expect(await screen.findByText("draft-invalid detail")).toBeVisible();
    expect(validated).toBe(false);

    await user.type(editor, "{Control>}s{/Control}");
    expect(await screen.findByText("Draft saved.")).toBeVisible();
    expect(puts).toBe(2);
    expect(screen.queryByText("draft-invalid detail")).toBeNull();
  });

  function conflictHandlers(state: {
    current: number;
    doc: string;
    docGets: number;
  }) {
    return [
      http.get(`${API}/drafts/d-1`, () =>
        HttpResponse.json(draft(state.current)),
      ),
      http.get(`${API}/drafts/d-1/document`, () => {
        state.docGets += 1;
        return new HttpResponse(state.doc, {
          headers: { "Content-Type": "application/yaml" },
        });
      }),
      http.put(`${API}/drafts/d-1`, () => {
        state.current = 5;
        state.doc = `${DOC}owner: someone\n`;
        return problem(409, "precondition-failed");
      }),
      ...base(),
    ];
  }

  it("discards my edits and refetches the draft from the conflict dialog", async () => {
    const user = userEvent.setup();
    const state = { current: 1, doc: DOC, docGets: 0 };
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: conflictHandlers(state),
    });
    const editor = await editorBox();
    await user.type(editor, "mine");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    const dialog = await screen.findByRole("dialog", {
      name: "This draft changed on the server",
    });
    expect(
      within(dialog).getByText(/Someone saved revision 5 \(by local:bob\)/),
    ).toBeVisible();
    // Both versions side by side: the server's current copy and the unsaved buffer.
    expect(
      within(dialog).getByLabelText("Server copy, revision 5"),
    ).toHaveTextContent("owner: someone");
    expect(
      within(dialog).getByLabelText("Server copy, revision 5"),
    ).not.toHaveTextContent("mine");
    expect(
      within(dialog).getByLabelText("Your unsaved edits"),
    ).toHaveTextContent("mine");
    expect(state.docGets).toBe(2);
    await user.click(
      within(dialog).getByRole("button", { name: "Reload latest" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await waitFor(() => {
      expect(state.docGets).toBe(3);
    });
    expect(((await editorBox()) as HTMLTextAreaElement).value).not.toContain(
      "mine",
    );
    expect(screen.getByText("Saved")).toBeVisible();
  });

  // Regression: "Reload latest" re-seeded from the still-cached old document and ETag before the
  // refetch landed, so the next save conflicted again.
  it("loads the newer server copy after Reload latest", async () => {
    const user = userEvent.setup();
    const state = { current: 1, doc: DOC, docGets: 0 };
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: conflictHandlers(state),
    });
    await user.type(await editorBox(), "mine");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Reload latest" }),
    );
    await waitFor(async () => {
      expect(await editorBox()).toHaveValue(`${DOC}owner: someone\n`);
    });
    expect(screen.getByText("Revision 5")).toBeVisible();
  });

  it("still offers both choices when the server copy cannot be loaded for comparison", async () => {
    const user = userEvent.setup();
    let docGets = 0;
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.get(`${API}/drafts/d-1/document`, () => {
          docGets += 1;
          return docGets === 1
            ? new HttpResponse(DOC, {
                headers: { "Content-Type": "application/yaml" },
              })
            : problem(500, "document-broken");
        }),
        http.put(`${API}/drafts/d-1`, () =>
          problem(412, "precondition-failed"),
        ),
      ]),
    });
    await user.type(await editorBox(), "x");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    const dialog = await screen.findByRole("dialog", {
      name: "This draft changed on the server",
    });
    expect(
      within(dialog).getByText(
        /The server copy could not be loaded for comparison/,
      ),
    ).toBeVisible();
    expect(within(dialog).queryByLabelText("Your unsaved edits")).toBeNull();
    expect(
      within(dialog).getByRole("button", { name: "Keep my edits" }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Reload latest" }),
    ).toBeVisible();
  });

  it("shows the problem when keeping my edits fails too", async () => {
    const user = userEvent.setup();
    let puts = 0;
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.put(`${API}/drafts/d-1`, () => {
          puts += 1;
          return puts === 1
            ? problem(412, "precondition-failed")
            : problem(500, "still-broken");
        }),
      ]),
    });
    await user.type(await editorBox(), "x");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Keep my edits" }),
    );
    expect(await screen.findByText("still-broken detail")).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("draft editor publishing", () => {
  it("publishes, leaves for the versions tab and names the new Preview", async () => {
    const user = userEvent.setup();
    let ifMatch: string | null = null;
    const { router } = renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.get(`${API}/diff`, ({ request }) => {
          const url = new URL(request.url);
          expect(url.searchParams.get("from")).toBe("latest");
          expect(url.searchParams.get("to")).toBe("d-1");
          expect(url.searchParams.get("limit")).toBe("1");
          return HttpResponse.json({
            required_bump: "none",
            counts: {},
            changes: [],
          });
        }),
        http.post(`${API}/drafts/d-1/publish`, ({ request }) => {
          ifMatch = request.headers.get("If-Match");
          return HttpResponse.json({ semver: "1.1.0", status: "preview" });
        }),
      ]),
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Publish 1.1.0?",
    });
    expect(await within(dialog).findByText("Needs no bump")).toBeVisible();
    expect(
      within(dialog).getByText("0 breaking, 0 warning, 0 info"),
    ).toBeVisible();
    expect(
      within(dialog).getByText(/publishes as a Preview; promote it/),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Publish 1.1.0" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/c-1/versions");
    });
    expect(router.state.location.search).toEqual({
      published: "1.1.0",
      status: "preview",
    });
    expect(ifMatch).toBe('"r1"');
    expect(await screen.findByText("Published 1.1.0 as Preview")).toBeVisible();
  });

  it.each([
    ["status: draft\n", "This document says status: draft."],
    ["status: proposed\n", "This document says status: proposed."],
    ["", "This document sets no status."],
  ])(
    "warns in the dialog that %j is not active and keeps Publish enabled",
    async (extra, lead) => {
      const user = userEvent.setup();
      renderApp("/contracts/c-1/drafts/d-1", {
        folders,
        permissions: ALL,
        handlers: base([], { docText: `${DOC}${extra}` }),
      });
      await user.click(await screen.findByRole("button", { name: "Publish" }));
      const dialog = await screen.findByRole("dialog", {
        name: "Publish 1.1.0?",
      });
      expect(
        within(dialog)
          .getAllByRole("status")
          .some((el) => el.textContent.includes(lead)),
      ).toBe(true);
      expect(dialog.textContent).toContain(
        `${lead} Consumers usually expect active for a published version.`,
      );
      expect(dialog.textContent).toContain("does not block publishing");
      expect(
        within(dialog).getByRole("button", { name: "Publish 1.1.0" }),
      ).toBeEnabled();
    },
  );

  it("shows no status warning in the dialog for an active document", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([], { docText: `${DOC}status: active\n` }),
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Publish 1.1.0?",
    });
    expect(dialog.textContent).not.toContain("Consumers usually expect");
  });

  it("explains a version conflict with the highest published version", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base(
        [
          http.post(`${API}/drafts/d-1/publish`, () =>
            problem(409, "semver-conflict", { highest_semver: "1.4.0" }),
          ),
        ],
        { contractBody: { ...contract, latest_version: null } },
      ),
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Publish 1.1.0?",
    });
    expect(
      within(dialog).getByText("None: this becomes the Latest"),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Publish 1.1.0" }),
    );
    expect(
      await screen.findByText("Publish refused: version conflict"),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Version 1.1.0 already exists or is below the highest (1.4.0). Choose a higher version.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens the conflict dialog when the draft changed before publishing", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base(
        [
          http.post(`${API}/drafts/d-1/publish`, () =>
            problem(412, "precondition-failed"),
          ),
        ],
        { contractBody: { ...contract, latest_version: null } },
      ),
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Publish 1.1.0",
      }),
    );
    expect(
      await screen.findByRole("dialog", {
        name: "This draft changed on the server",
      }),
    ).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("keeps the publish dialog open with any other failure, and closes on Keep editing", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.get(`${API}/diff`, () => problem(500, "diff-broken")),
        http.post(`${API}/drafts/d-1/publish`, () =>
          problem(400, "publish-broken"),
        ),
      ]),
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      await within(dialog).findByText("Comparison unavailable"),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Publish 1.1.0" }),
    );
    expect(
      await within(dialog).findByText("publish-broken detail"),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Keep editing" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("labels a draft without a declared version and copies the gate run id", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base(
        [
          http.get(`${API}/diff`, () =>
            HttpResponse.json({
              required_bump: "major",
              counts: { breaking: 2, warning: 1, info: 3 },
              changes: [],
            }),
          ),
          http.post(`${API}/drafts/d-1/publish`, () =>
            problem(422, "contract-validation-failed", {
              run_id: "run-9",
              report: {
                findings: [
                  {
                    source: "rule",
                    severity: "error",
                    outcome: "fail",
                    message: "One.",
                    blocking: true,
                  },
                  {
                    source: "rule",
                    severity: "error",
                    outcome: "fail",
                    message: "Two.",
                    blocking: true,
                  },
                ],
                summary: { publishable: false, blocking_count: 2 },
              },
            }),
          ),
        ],
        { meta: draft(1, { declared_version: null }) },
      ),
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Publish draft?",
    });
    expect(within(dialog).getByText("Not set")).toBeVisible();
    expect(await within(dialog).findByText("Needs a major bump")).toBeVisible();
    expect(
      within(dialog).getByText("2 breaking, 1 warning, 3 info"),
    ).toBeVisible();
    // No declared version: no bump comparison, so no warning.
    expect(within(dialog).queryByText(/you declared/)).toBeNull();
    await user.click(
      within(dialog).getByRole("button", { name: "Publish draft" }),
    );
    expect(
      await screen.findByText("Publish blocked: 2 blocking findings"),
    ).toBeVisible();
    expect(screen.getByText("run-9")).toBeVisible();
    expect(
      screen.getByRole("region", { name: "Publish gate result" }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Copy run ID" }));
    expect(await navigator.clipboard.readText()).toBe("run-9");
  });

  it("shows a gate refusal without a run id", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base(
        [
          http.post(`${API}/drafts/d-1/publish`, () =>
            problem(422, "contract-validation-failed", {
              report: { summary: { blocking_count: 1 } },
            }),
          ),
        ],
        { contractBody: { ...contract, latest_version: null } },
      ),
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Publish 1.1.0",
      }),
    );
    expect(
      await screen.findByText("Publish blocked: 1 blocking finding"),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Copy run ID" })).toBeNull();
  });
});

describe("draft editor discard, leave guard and download", () => {
  it("discards with If-Match and returns to the drafts list", async () => {
    const user = userEvent.setup();
    let ifMatch: string | null = null;
    const { router } = renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.delete(`${API}/drafts/d-1`, ({ request }) => {
          ifMatch = request.headers.get("If-Match");
          return new HttpResponse(null, { status: 204 });
        }),
      ]),
    });
    await editorBox();
    await user.click(screen.getByRole("button", { name: "Discard draft" }));
    const confirm = await screen.findByRole("alertdialog", {
      name: "Discard draft 1.1.0?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Discard draft" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/c-1/drafts");
    });
    expect(ifMatch).toBe('"r1"');
  });

  it("asks before leaving with unsaved changes", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base(),
    });
    await user.type(await editorBox(), "x");
    const sections = screen.getByRole("navigation", {
      name: "Contract sections",
    });
    await user.click(within(sections).getByRole("link", { name: "Versions" }));
    expect(
      await screen.findByRole("dialog", { name: "Leave without saving?" }),
    ).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await user.click(within(sections).getByRole("link", { name: "Versions" }));
    let dialog = await screen.findByRole("dialog", {
      name: "Leave without saving?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Keep editing" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(router.state.location.pathname).toBe("/contracts/c-1/drafts/d-1");
    expect(await editorBox()).toHaveValue(`${DOC}x`);

    await user.click(within(sections).getByRole("link", { name: "Versions" }));
    dialog = await screen.findByRole("dialog", {
      name: "Leave without saving?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Leave without saving" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/c-1/versions");
    });
  });

  it("downloads the current buffer under the draft's name", async () => {
    const user = userEvent.setup();
    const saved: string[] = [];
    let blob: Blob | null = null;
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn((value: Blob) => {
        blob = value;
        return "blob:test";
      }),
    });
    const revoke = vi.fn();
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: revoke,
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      saved.push(this.download);
    });
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base(),
    });
    await user.type(await editorBox(), "x");
    await user.click(screen.getByRole("button", { name: "Download buffer" }));
    expect(saved).toEqual(["urn-acme-ledger-orders-draft-1.1.0.yaml"]);
    expect(await (blob as unknown as Blob).text()).toBe(`${DOC}x`);
    expect((blob as unknown as Blob).type).toBe("application/yaml");
    await waitFor(() => {
      expect(revoke).toHaveBeenCalledWith("blob:test");
    });
  });
});

describe("contract api helpers", () => {
  it("fetches documents as text with the requested Accept and reads the headers", async () => {
    const seen: string[] = [];
    const respond =
      (body: string, type?: string) =>
      ({ request }: { request: Request }) => {
        seen.push(
          `${new URL(request.url).pathname} ${request.headers.get("Accept") ?? ""}`,
        );
        return new HttpResponse(body, {
          headers: {
            ...(type ? { "Content-Type": type } : {}),
            ETag: '"t"',
          },
        });
      };
    server.use(
      http.get(`${API}/document`, respond("a: 1\n", "application/yaml")),
      http.get(
        `${API}/drafts/d-1/document`,
        respond('{"a":1}', "application/json; charset=utf-8"),
      ),
      http.get(`${API}/versions/1.0.0`, respond("")),
    );
    const api = createApiClient(testConfig);
    expect(await fetchContractDocument(api, "c-1")).toEqual({
      text: "a: 1\n",
      etag: '"t"',
      mediaType: "application/yaml",
    });
    expect(await fetchDraftDocument(api, "c-1", "d-1", "json")).toEqual({
      text: '{"a":1}',
      etag: '"t"',
      mediaType: "application/json",
    });
    const version = await fetchVersionDocument(api, "c-1", "1.0.0", "yaml");
    expect(version.text).toBe("");
    expect(version.etag).toBe('"t"');
    // No format asked: the client's default Accept, so the stored format comes back.
    expect(seen[0]).toBe("/api/v1/contracts/contracts/c-1/document */*");
    expect(seen[1]).toBe(
      "/api/v1/contracts/contracts/c-1/drafts/d-1/document application/json",
    );
    expect(seen[2]).toBe(
      "/api/v1/contracts/contracts/c-1/versions/1.0.0 application/yaml",
    );
  });

  it("formats names, media types, URLs and principals", () => {
    expect(formatOf("application/json")).toBe("json");
    expect(formatOf("application/yaml")).toBe("yaml");
    expect(formatOf(null)).toBe("yaml");
    expect(mediaTypeOf("json")).toBe("application/json");
    expect(mediaTypeOf("yaml")).toBe("application/yaml");
    expect(fileNameFor("urn:acme/orders v1", "1.0.0", "json")).toBe(
      "urn-acme-orders-v1-1.0.0.json",
    );
    expect(fileNameFor("::", "draft", "yaml")).toBe("contract-draft.yaml");
    expect(absoluteUrl("/api/v1/x")).toBe(`${window.location.origin}/api/v1/x`);
    expect(shortPrincipal(null)).toBe("Not set");
    expect(shortPrincipal("")).toBe("Not set");
    expect(shortPrincipal("bob")).toBe("bob");
    expect(shortPrincipal("local:1234567890ab")).toBe("local:12345678");
  });

  it("reads the ids of an odcs-id-mismatch problem only", () => {
    const mismatch = (
      extensions?: Record<string, unknown>,
      slug = "odcs-id-mismatch",
    ): Problem => ({
      kind: "problem",
      status: 422,
      slug,
      title: "t",
      detail: null,
      errors: [],
      requestId: null,
      ...(extensions ? { extensions } : {}),
    });
    expect(
      idMismatchOf(mismatch({ expected_odcs_id: "a", found_odcs_id: "b" })),
    ).toEqual({ expected: "a", found: "b" });
    expect(idMismatchOf(mismatch({ expected_odcs_id: "a" }))).toEqual({
      expected: "a",
      found: null,
    });
    expect(idMismatchOf(mismatch())).toBeNull();
    expect(idMismatchOf(mismatch({ expected_odcs_id: 5 }))).toBeNull();
    expect(idMismatchOf(mismatch({ expected_odcs_id: "a" }, "x"))).toBeNull();
    expect(idMismatchOf(null)).toBeNull();
  });

  it("words version actions and the semver callouts", () => {
    expect(todayUtc()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(curlExample("https://x/v")).toBe(
      'curl -H "Authorization: Bearer $DC_TOKEN" -H "Accept: application/yaml" https://x/v',
    );
    const conflict: Problem = {
      kind: "problem",
      status: 409,
      slug: "semver-conflict",
      title: "Conflict",
      detail: null,
      errors: [],
      requestId: null,
      extensions: {},
    };
    expect(semverConflictCopy(conflict, null)).toBe(
      "Version this version already exists or is below the highest. Choose a higher version.",
    );
    expect(breakingCopy("No version here.", null)).toBe(
      "Breaking changes need a new major version. Declared this version.",
    );
  });

  it("compares declared versions for the bump", () => {
    expect(declaredBump("1.0.0", "2.0.0")).toBe("major");
    expect(declaredBump("1.0.0", "1.1.0")).toBe("minor");
    expect(declaredBump("1.0.0", "1.0.1")).toBe("patch");
    expect(declaredBump("1.0.0", "1.0.0")).toBe("none");
    expect(declaredBump(null, "1.0.0")).toBeNull();
    expect(declaredBump("1.0.0", "next")).toBeNull();
  });

  it("reads the ODCS status of the saved text for the publish hint", () => {
    expect(documentStatus(null)).toBeUndefined();
    expect(documentStatus(undefined)).toBeUndefined();
    expect(documentStatus("")).toBeUndefined();
    expect(documentStatus("  \n")).toBeUndefined();
    expect(documentStatus("a: [unclosed")).toBeUndefined();
    expect(documentStatus("- status: draft")).toBeUndefined();
    expect(documentStatus("just text")).toBeUndefined();
    expect(documentStatus("version: 1.0.0\n")).toBeNull();
    expect(documentStatus("status:\n")).toBeNull();
    expect(documentStatus("status: draft\n")).toBe("draft");
    expect(documentStatus('{"status":"active"}')).toBe("active");
    expect(documentStatus("status: 1\n")).toBe("1");
  });

  it("turns locatable findings into editor markers by group", () => {
    const finding = (
      id: string,
      group: UnifiedFinding["group"],
      pointer: string | null,
      ruleKey: string | null = null,
    ): UnifiedFinding => ({
      id,
      group,
      severity: "Error",
      source: "acme",
      ruleKey,
      message: `m-${id}`,
      pointer,
      mode: null,
      paused: false,
    });
    const range = {
      line: 2,
      column: 1,
      endLine: 2,
      endColumn: 5,
      atParent: false,
    };
    const markers = markersFor(
      [
        finding("a", "blocking", "/a", "rule-a"),
        finding("b", "warning", "/b"),
        finding("c", "info", "/c"),
        finding("d", "info", null),
        finding("e", "info", "/missing"),
      ],
      (pointer) => (pointer === "/missing" ? null : range),
    );
    expect(markers).toEqual([
      {
        line: 2,
        column: 1,
        endLine: 2,
        endColumn: 5,
        severity: "error",
        message: "acme · rule-a: m-a",
      },
      {
        line: 2,
        column: 1,
        endLine: 2,
        endColumn: 5,
        severity: "warning",
        message: "acme: m-b",
      },
      {
        line: 2,
        column: 1,
        endLine: 2,
        endColumn: 5,
        severity: "info",
        message: "acme: m-c",
      },
    ]);
  });
});

describe("draft editor quiet paths and bodiless answers", () => {
  const noContent = () => new HttpResponse(null, { status: 204 });

  it("shows a bodiless draft read as a problem", async () => {
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([http.get(`${API}/drafts/d-1`, noContent)]),
    });
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(
      screen.queryByRole("textbox", { name: "Draft 1.1.0 document" }),
    ).toBeNull();
  });

  it("sends nothing on Ctrl+S with a clean buffer, and reports a bodiless save", async () => {
    const user = userEvent.setup();
    let puts = 0;
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.put(`${API}/drafts/d-1`, () => {
          puts += 1;
          return noContent();
        }),
      ]),
    });
    const editor = await editorBox();
    fireEvent.keyDown(editor, { key: "s", ctrlKey: true });
    expect(puts).toBe(0);
    await user.type(editor, "x");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(puts).toBe(1);
    expect(screen.queryByText("Draft saved.")).toBeNull();
  });

  it("lists nothing when a validate answers without a body", async () => {
    const user = userEvent.setup();
    let validated = 0;
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.post(`${API}/drafts/d-1/validate`, () => {
          validated += 1;
          return noContent();
        }),
      ]),
    });
    await user.click(
      await screen.findByRole("button", { name: "Validate draft" }),
    );
    await waitFor(() => {
      expect(validated).toBe(1);
    });
    expect(
      screen.getByRole("button", { name: "Validate draft" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("region", { name: "Validation findings" }),
    ).toBeNull();
  });

  it("names only the contract's id when the problem does not say what the document has", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.put(`${API}/drafts/d-1`, () =>
          problem(422, "odcs-id-mismatch", {
            expected_odcs_id: "urn:acme:ledger:orders",
          }),
        ),
      ]),
    });
    await user.type(await editorBox(), "x");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "This contract's id is urn:acme:ledger:orders.",
    );
    expect(alert).not.toHaveTextContent("The document says");
  });

  it("stays on the editor when a publish answers without a body", async () => {
    const user = userEvent.setup();
    let published = 0;
    const { router } = renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.get(`${API}/diff`, () =>
          HttpResponse.json({ required_bump: "none", counts: {}, changes: [] }),
        ),
        http.post(`${API}/drafts/d-1/publish`, () => {
          published += 1;
          return noContent();
        }),
      ]),
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Publish 1.1.0?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Publish 1.1.0" }),
    );
    await waitFor(() => {
      expect(published).toBe(1);
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(router.state.location.pathname).toBe("/contracts/c-1/drafts/d-1");
  });

  it("keeps the publish dialog open with a network failure", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([
        http.get(`${API}/diff`, () =>
          HttpResponse.json({ required_bump: "none", counts: {}, changes: [] }),
        ),
        http.post(`${API}/drafts/d-1/publish`, () => HttpResponse.error()),
      ]),
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Publish 1.1.0?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Publish 1.1.0" }),
    );
    expect(
      await within(dialog).findByText("Can't reach the server"),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Publish 1.1.0" }),
    ).toBeVisible();
  });

  it("names a draft without a declared version in downloads and full screen", async () => {
    const user = userEvent.setup();
    const saved: string[] = [];
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
      saved.push(this.download);
    });
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ALL,
      handlers: base([], { meta: draft(1, { declared_version: null }) }),
    });
    await screen.findByRole("textbox", { name: "Draft document" });
    await user.click(screen.getByRole("button", { name: "Download buffer" }));
    expect(saved).toEqual(["urn-acme-ledger-orders-draft-unversioned.yaml"]);
    await user.click(screen.getByRole("button", { name: "Expand editor" }));
    expect(
      await screen.findByRole("dialog", { name: "Orders · Draft" }),
    ).toBeVisible();
  });
});
