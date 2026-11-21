// The full-screen editor ("Expand editor"): the Document tab viewer opens in a dialog with the
// format toggle and downloads, closes with Escape or "Exit full screen", and gives focus back to
// "Expand editor"; the format choice is shared with the page. The draft editor's dialog edits the
// page's buffer: the dirty state survives open and close, closing never saves, and Save draft or
// Ctrl+S inside it saves with If-Match.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder } from "@/test/renderApp";

vi.setConfig({ testTimeout: 30000 });

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
  version: { number: 2, state: "draft" },
  latest_version: { version_id: "v-1", semver: "1.0.0" },
  created_by: "local:admin",
  created_at: "2026-11-01T10:00:00Z",
  updated_by: "local:admin",
  updated_at: "2026-11-02T10:00:00Z",
};

const DOC =
  "apiVersion: v3.1.0\nkind: DataContract\nid: urn:acme:ledger:orders\nversion: 1.1.0\n";
const CONVERTED = "# converted to YAML\napiVersion: v3.1.0\n";

function documentHandlers(accepts: (string | null)[]) {
  return [
    http.get("*/api/v1/contracts/contracts/c-1", () =>
      HttpResponse.json(contract),
    ),
    http.get("*/api/v1/contracts/contracts/c-1/document", ({ request }) => {
      const accept = request.headers.get("Accept");
      accepts.push(accept);
      return new HttpResponse(accept === "application/yaml" ? CONVERTED : DOC, {
        headers: { "Content-Type": "application/yaml" },
      });
    }),
  ];
}

describe("expanded Document viewer", () => {
  it("opens full screen with the toggle and downloads, and closes with Escape or Exit full screen", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/document", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: documentHandlers([]),
    });
    expect(await screen.findByLabelText("Contract document")).toHaveValue(DOC);
    const expand = screen.getByRole("button", { name: "Expand editor" });

    await user.click(expand);
    const dialog = await screen.findByRole("dialog", {
      name: "Orders · Document",
    });
    expect(
      within(dialog).getByRole("textbox", { name: "Contract document" }),
    ).toHaveValue(DOC);
    expect(
      within(dialog).getByRole("group", { name: "Document format" }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Download YAML" }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Download JSON" }),
    ).toBeVisible();
    // The page editor is unmounted while the dialog shows the document.
    expect(screen.getAllByLabelText("Contract document")).toHaveLength(1);

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(screen.getByRole("button", { name: "Expand editor" })).toHaveFocus();
    expect(screen.getByLabelText("Contract document")).toHaveValue(DOC);

    await user.click(screen.getByRole("button", { name: "Expand editor" }));
    const again = await screen.findByRole("dialog", {
      name: "Orders · Document",
    });
    await user.click(
      within(again).getByRole("button", { name: "Exit full screen" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(screen.getByRole("button", { name: "Expand editor" })).toHaveFocus();
  });

  it("shares the format choice with the page", async () => {
    const user = userEvent.setup();
    const accepts: (string | null)[] = [];
    renderApp("/contracts/c-1/document", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: documentHandlers(accepts),
    });
    await screen.findByLabelText("Contract document");
    await user.click(screen.getByRole("button", { name: "Expand editor" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Orders · Document",
    });
    await user.click(within(dialog).getByRole("button", { name: "YAML" }));
    await waitFor(() => {
      expect(
        within(dialog).getByRole("textbox", { name: "Contract document" }),
      ).toHaveValue(CONVERTED);
    });
    expect(accepts).toContain("application/yaml");

    await user.click(
      within(dialog).getByRole("button", { name: "Exit full screen" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(screen.getByRole("button", { name: "YAML" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByLabelText("Contract document")).toHaveValue(CONVERTED);
  });
});

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

type Put = { ifMatch: string | null; body: string };

function draftHandlers(puts: Put[]) {
  return [
    http.get("*/api/v1/contracts/contracts/c-1", () =>
      HttpResponse.json(contract),
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
    http.put(
      "*/api/v1/contracts/contracts/c-1/drafts/d-1",
      async ({ request }) => {
        puts.push({
          ifMatch: request.headers.get("If-Match"),
          body: await request.text(),
        });
        return HttpResponse.json({
          draft: draft(2),
          structural_validation: { valid: true, error_count: 0, errors: [] },
          validation: { status: "unavailable" },
        });
      },
    ),
  ];
}

const DRAFT_TITLE = "Orders · Draft 1.1.0";
const DRAFT_LABEL = "Draft 1.1.0 document";

describe("expanded draft editor", () => {
  it("edits the page buffer: unsaved edits survive closing and nothing is saved", async () => {
    const user = userEvent.setup();
    const puts: Put[] = [];
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: draftHandlers(puts),
    });
    await screen.findByRole("textbox", { name: DRAFT_LABEL });
    await user.click(screen.getByRole("button", { name: "Expand editor" }));
    const dialog = await screen.findByRole("dialog", { name: DRAFT_TITLE });
    const area = within(dialog).getByRole("textbox", { name: DRAFT_LABEL });
    expect(area).toHaveValue(DOC);
    expect(within(dialog).getByText("Saved")).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Validate draft" }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Download buffer" }),
    ).toBeVisible();
    // Publish and Discard open dialogs of their own, so they stay on the page.
    expect(
      within(dialog).queryByRole("button", { name: "Publish" }),
    ).toBeNull();
    expect(
      within(dialog).queryByRole("button", { name: "Discard draft" }),
    ).toBeNull();

    await user.type(area, "status: active");
    expect(within(dialog).getByText("Unsaved changes")).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Save and validate" }),
    ).toBeVisible();

    await user.click(
      within(dialog).getByRole("button", { name: "Exit full screen" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(screen.getByRole("textbox", { name: DRAFT_LABEL })).toHaveValue(
      `${DOC}status: active`,
    );
    expect(screen.getByText("Unsaved changes")).toBeVisible();
    expect(screen.getByRole("button", { name: "Expand editor" })).toHaveFocus();
    expect(puts).toEqual([]);
  });

  it("saves from the dialog with If-Match, by button and by Ctrl+S, and stays open", async () => {
    const user = userEvent.setup();
    const puts: Put[] = [];
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: CONTRACT_PERMS,
      handlers: draftHandlers(puts),
    });
    await screen.findByRole("textbox", { name: DRAFT_LABEL });
    await user.click(screen.getByRole("button", { name: "Expand editor" }));
    const dialog = await screen.findByRole("dialog", { name: DRAFT_TITLE });
    const area = within(dialog).getByRole("textbox", { name: DRAFT_LABEL });
    await user.type(area, "x");
    await user.click(
      within(dialog).getByRole("button", { name: "Save draft" }),
    );
    expect(await within(dialog).findByText("Draft saved.")).toBeVisible();
    expect(puts).toEqual([{ ifMatch: '"r1"', body: `${DOC}x` }]);
    expect(within(dialog).getByText("Saved")).toBeVisible();
    expect(screen.getByRole("dialog", { name: DRAFT_TITLE })).toBeVisible();

    await user.type(area, "y");
    fireEvent.keyDown(area, { key: "s", ctrlKey: true });
    await waitFor(() => {
      expect(puts).toHaveLength(2);
    });
    expect(puts[1]).toEqual({ ifMatch: '"r2"', body: `${DOC}xy` });

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(screen.getByRole("button", { name: "Expand editor" })).toHaveFocus();
  });

  it("is read-only without contract:update", async () => {
    const user = userEvent.setup();
    renderApp("/contracts/c-1/drafts/d-1", {
      folders,
      permissions: ["contract:read"],
      handlers: draftHandlers([]),
    });
    await screen.findByRole("textbox", { name: DRAFT_LABEL });
    await user.click(screen.getByRole("button", { name: "Expand editor" }));
    const dialog = await screen.findByRole("dialog", { name: DRAFT_TITLE });
    expect(
      within(dialog).getByRole("textbox", { name: DRAFT_LABEL }),
    ).toHaveAttribute("readonly");
    expect(
      within(dialog).queryByRole("button", { name: "Save draft" }),
    ).toBeNull();
  });
});
