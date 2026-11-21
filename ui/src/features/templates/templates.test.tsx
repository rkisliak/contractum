// Templates through the real router: the list, creating a template (raw YAML body), the
// template page publishing the draft and deprecating a version, and the folder Templates tab.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Template, TemplateVersion } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder } from "@/test/renderApp";

vi.setConfig({ testTimeout: 20000 });

afterEach(() => {
  tokenStore.clear();
});

const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const folders = [rootFolder, acme];
const PERMISSIONS = [
  "template:read",
  "template:create",
  "template:update",
  "template:publish",
  "template:deprecate",
];

function template(overrides: Partial<Template> = {}): Template {
  return {
    id: "t-1",
    folder_id: "acme-id",
    organization_id: "acme-id",
    name: "Batch table",
    publish_counter: 1,
    latest_version_no: 1,
    draft_redacted: false,
    draft_api_version: "v3.1.0",
    draft_media_type: "application/yaml",
    draft_content_sha256: "abc",
    draft_size_bytes: 10,
    created_by: "local:admin",
    created_at: "2026-11-01T10:00:00Z",
    updated_by: "local:admin",
    updated_at: "2026-11-02T10:00:00Z",
    ...overrides,
  };
}

function version(n: number, deprecated = false): TemplateVersion {
  return {
    id: `tv-${String(n)}`,
    template_id: "t-1",
    version_no: n,
    name: "Batch table",
    odcs_api_version: "v3.1.0",
    media_type: "application/yaml",
    content_sha256: "abc",
    size_bytes: 10,
    deprecated,
    deprecated_at: deprecated ? "2026-11-03T10:00:00Z" : null,
    deprecated_by: deprecated ? "local:admin" : null,
    created_by: "local:admin",
    created_at: "2026-11-02T10:00:00Z",
  };
}

const report = { valid: true, error_count: 0, errors: [] };

describe("templates list", () => {
  it("lists templates with their owning folder and opens one", async () => {
    renderApp("/templates", {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/contracts/templates", () =>
          HttpResponse.json({ items: [template()], next_cursor: null }),
        ),
      ],
    });
    const link = await screen.findByRole("link", { name: "Batch table" });
    expect(link).toHaveAttribute("href", "/templates/t-1");
    const row = link.closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("acme")).toBeVisible();
  });

  it("creates a template from a YAML document and opens it", async () => {
    const user = userEvent.setup();
    const sent: { type: string | null; name: string | null; body: string }[] =
      [];
    const { router } = renderApp("/templates?folder_id=acme-id", {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/contracts/templates", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
        http.post(
          "*/api/v1/contracts/folders/acme-id/templates",
          async ({ request }) => {
            sent.push({
              type: request.headers.get("Content-Type"),
              name: new URL(request.url).searchParams.get("name"),
              body: await request.text(),
            });
            return HttpResponse.json(
              {
                template: template({ id: "t-new" }),
                structural_validation: report,
              },
              { status: 201 },
            );
          },
        ),
        http.get("*/api/v1/contracts/templates/t-new", () =>
          HttpResponse.json(template({ id: "t-new" })),
        ),
        http.get("*/api/v1/contracts/templates/t-new/document", () =>
          HttpResponse.text("kind: DataContract\n"),
        ),
        http.get("*/api/v1/contracts/templates/t-new/versions", () =>
          HttpResponse.json({ items: [] }),
        ),
      ],
    });
    const buttons = await screen.findAllByRole("button", {
      name: "Create template",
    });
    await user.click(buttons[0] as HTMLElement);
    const dialog = await screen.findByRole("dialog", {
      name: "Create template",
    });
    await user.type(within(dialog).getByLabelText("Name"), "Orders template");
    await user.click(
      within(dialog).getByRole("button", { name: "Create template" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/templates/t-new");
    });
    expect(sent[0]).toMatchObject({
      type: "application/yaml",
      name: "Orders template",
    });
    expect(sent[0]?.body).toContain("kind: DataContract");
  });
});

describe("template page", () => {
  function handlers(onPublish: () => void, onDeprecate: () => void) {
    return [
      http.get("*/api/v1/contracts/templates/t-1", () =>
        HttpResponse.json(template()),
      ),
      http.get("*/api/v1/contracts/templates/t-1/document", () =>
        HttpResponse.text("apiVersion: v3.1.0\nkind: DataContract\n"),
      ),
      http.get("*/api/v1/contracts/templates/t-1/versions", () =>
        HttpResponse.json({ items: [version(1)] }),
      ),
      http.post("*/api/v1/contracts/templates/t-1/publish", () => {
        onPublish();
        return HttpResponse.json(
          {
            template: template({ publish_counter: 2 }),
            version: version(2),
            structural_validation: report,
          },
          { status: 201 },
        );
      }),
      http.post("*/api/v1/contracts/templates/t-1/versions/1/deprecate", () => {
        onDeprecate();
        return HttpResponse.json(version(1, true));
      }),
    ];
  }

  it("shows the draft and publishes it after confirmation", async () => {
    const user = userEvent.setup();
    let published = 0;
    renderApp("/templates/t-1", {
      folders,
      permissions: PERMISSIONS,
      handlers: handlers(
        () => (published += 1),
        () => undefined,
      ),
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Batch table" }),
    ).toBeVisible();
    expect(
      await screen.findByLabelText("Draft document (YAML or JSON)"),
    ).toHaveValue("apiVersion: v3.1.0\nkind: DataContract\n");
    await user.click(
      await screen.findByRole("button", { name: "Publish template" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Publish template version 2?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Publish template" }),
    );
    expect(await screen.findByText(/Version 2 is published/)).toBeVisible();
    expect(published).toBe(1);
  });

  it("deprecates a version from the Versions tab", async () => {
    const user = userEvent.setup();
    let deprecated = 0;
    renderApp("/templates/t-1", {
      folders,
      permissions: PERMISSIONS,
      handlers: handlers(
        () => undefined,
        () => (deprecated += 1),
      ),
    });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    await user.click(
      await screen.findByRole("button", { name: "Deprecate version 1" }),
    );
    const confirm = await screen.findByRole("alertdialog");
    await user.click(
      within(confirm).getByRole("button", { name: "Deprecate version" }),
    );
    await waitFor(() => {
      expect(deprecated).toBe(1);
    });
  });

  it("renders NotFound for an unknown template", async () => {
    renderApp("/templates/missing", {
      folders,
      handlers: [
        http.get("*/api/v1/contracts/templates/missing", () =>
          HttpResponse.json(
            {
              type: "urn:dc:problem:not-found",
              title: "Not Found",
              status: 404,
            },
            {
              status: 404,
              headers: { "Content-Type": "application/problem+json" },
            },
          ),
        ),
      ],
    });
    expect(
      await screen.findByText(/does not exist, or you do not have access/),
    ).toBeVisible();
  });
});

describe("folder templates tab", () => {
  it("lists templates defined here and available here", async () => {
    renderApp("/folders/acme-id/templates", {
      folders,
      permissions: [...PERMISSIONS, "contract:create"],
      handlers: [
        http.get("*/api/v1/contracts/templates", () =>
          HttpResponse.json({ items: [template()], next_cursor: null }),
        ),
        http.get("*/api/v1/contracts/folders/acme-id/available-templates", () =>
          HttpResponse.json({
            items: [
              {
                template_id: "t-2",
                name: "Inherited table",
                folder_id: rootFolder.id,
                latest_version_no: 2,
                version_nos: [1, 2],
              },
              {
                template_id: "t-3",
                name: "Hidden owner table",
                folder_id: "folder-you-cannot-see",
                latest_version_no: 1,
                version_nos: [1],
              },
            ],
            next_cursor: null,
          }),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "Defined here" }),
    ).toBeVisible();
    expect(
      await screen.findByRole("link", { name: "Batch table" }),
    ).toBeVisible();
    expect(
      await screen.findByRole("link", { name: "Inherited table" }),
    ).toBeVisible();
    // A template owned by a folder the caller cannot see is named without a link (its page would
    // answer Not found), and its owner is muted body text, not code.
    expect(screen.getByText("Hidden owner table").closest("a")).toBeNull();
    const hidden = screen.getByText("Folder you cannot see");
    expect(hidden.tagName).toBe("SPAN");
    expect(hidden.closest("code")).toBeNull();
    const sections = screen.getByRole("navigation", {
      name: "Folder sections",
    });
    expect(
      within(sections).getByRole("link", { name: "Templates" }),
    ).toHaveAttribute("aria-current", "page");
  });
});

describe("folder templates tab without template:read", () => {
  it("shows a calm note when neither templates nor new contracts are allowed", async () => {
    renderApp("/folders/acme-id/templates", {
      folders,
      permissions: ["contract:read"],
    });
    expect(
      await screen.findByRole("heading", {
        name: "Not available at your access level",
      }),
    ).toBeVisible();
    // The tab itself is hidden once the capabilities are known.
    const sections = screen.getByRole("navigation", {
      name: "Folder sections",
    });
    expect(
      within(sections).queryByRole("link", { name: "Templates" }),
    ).toBeNull();
  });

  it("leaves out Defined here and lists only the available templates", async () => {
    renderApp("/folders/acme-id/templates", {
      folders,
      permissions: ["contract:create"],
      handlers: [
        http.get("*/api/v1/contracts/folders/acme-id/available-templates", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", { name: "Available here" }),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Defined here" })).toBeNull();
  });
});

describe("the name rule on the create template dialog", () => {
  it("shows the message for an emoji and sends nothing", async () => {
    const user = userEvent.setup();
    let posted = false;
    renderApp("/templates?folder_id=acme-id", {
      folders,
      permissions: PERMISSIONS,
      handlers: [
        http.get("*/api/v1/contracts/templates", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
        http.post("*/api/v1/contracts/folders/acme-id/templates", () => {
          posted = true;
          return HttpResponse.json({}, { status: 201 });
        }),
      ],
    });
    const buttons = await screen.findAllByRole("button", {
      name: "Create template",
    });
    await user.click(buttons[0] as HTMLElement);
    const dialog = await screen.findByRole("dialog", {
      name: "Create template",
    });
    await user.type(within(dialog).getByLabelText("Name"), "Orders 😀");
    expect(
      within(dialog).getByText(
        "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.",
      ),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Create template" }),
    );
    expect(posted).toBe(false);
  });
});
