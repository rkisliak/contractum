// /templates/$templateId through the real router, beyond the happy paths in templates.test.tsx:
// loading and problem states, the redacted reader view, capability-gated editing, saving the
// draft (raw body, media type, rename, schema report, failure), publish failure, and the
// Versions tab (empty, problem, viewing a version, deprecated rows, deprecate failure).
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Template, TemplateVersion } from "@/api/types";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { makeFolder, renderApp, rootFolder } from "@/test/renderApp";

vi.setConfig({ testTimeout: 60000 });

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
const ALL = [
  "template:read",
  "template:create",
  "template:update",
  "template:publish",
  "template:deprecate",
];
const DOC = "apiVersion: v3.1.0\nkind: DataContract\n";

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
    name: `Batch table v${String(n)}`,
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

const problem = (status: number, title: string) =>
  HttpResponse.json(
    { type: "urn:dc:problem:test", title, status },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );

function base({
  tpl = template(),
  doc = () => HttpResponse.text(DOC),
  versions = () => HttpResponse.json({ items: [version(1)] }) as Response,
}: {
  tpl?: Template;
  doc?: () => Response;
  versions?: () => Response;
} = {}): RequestHandler[] {
  return [
    http.get("*/api/v1/contracts/templates/t-1", () => HttpResponse.json(tpl)),
    http.get("*/api/v1/contracts/templates/t-1/document", doc),
    http.get("*/api/v1/contracts/templates/t-1/versions", versions),
  ];
}

describe("template page states", () => {
  it("shows a problem for the template and retries it", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        http.get("*/api/v1/contracts/templates/t-1", () => {
          calls += 1;
          return calls === 1
            ? problem(500, "Templates are down")
            : HttpResponse.json(template());
        }),
        ...base(),
      ],
    });
    expect(await screen.findByText("Templates are down")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Batch table" }),
    ).toBeVisible();
    expect(calls).toBe(2);
  });

  it("renders the header with plural versions and the owning folder path", async () => {
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: base({ tpl: template({ publish_counter: 3 }) }),
    });
    expect(await screen.findByText("3 published versions")).toBeVisible();
    expect(await screen.findByText("acme", { selector: "span" })).toBeVisible();
  });

  it("gives a reader of published versions only the Versions tab", async () => {
    renderApp("/templates/t-1", {
      folders,
      permissions: ["template:read"],
      handlers: base({
        tpl: template({ draft_redacted: true, publish_counter: 0 }),
      }),
    });
    expect(await screen.findByText("0 published versions")).toBeVisible();
    expect(screen.queryByRole("tab", { name: "Draft" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Versions" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      await screen.findByRole("button", { name: "View version 1" }),
    ).toBeVisible();
    // Without template:deprecate the row offers no deprecate action.
    expect(
      screen.queryByRole("button", { name: "Deprecate version 1" }),
    ).toBeNull();
  });
});

describe("template draft tab", () => {
  it("shows the draft read-only without template:update or template:publish", async () => {
    renderApp("/templates/t-1", {
      folders,
      permissions: ["template:read"],
      handlers: base({
        tpl: template({ draft_api_version: null, draft_media_type: null }),
      }),
    });
    const editor = await screen.findByLabelText(
      "Draft document (YAML or JSON)",
    );
    expect(editor).toHaveValue(DOC);
    expect(editor).toHaveAttribute("readonly");
    expect(screen.getByLabelText("Name")).toHaveAttribute("readonly");
    expect(screen.getByText("ODCS · stored as unknown")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save draft" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Publish template" }),
    ).toBeNull();
  });

  it("shows a problem for the draft document and retries it", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: base({
        doc: () => {
          calls += 1;
          return calls === 1
            ? problem(500, "Document unavailable")
            : HttpResponse.text(DOC);
        },
      }),
    });
    expect(await screen.findByText("Document unavailable")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByLabelText("Draft document (YAML or JSON)"),
    ).toHaveValue(DOC);
  });

  it("treats an empty document body as an empty draft", async () => {
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: base({
        doc: () => new HttpResponse(null, { status: 204 }),
      }),
    });
    expect(
      await screen.findByLabelText("Draft document (YAML or JSON)"),
    ).toHaveValue("");
  });

  it("saves an edited JSON draft with a new name and reports schema problems", async () => {
    const user = userEvent.setup();
    const sent: { url: URL; type: string | null; body: string }[] = [];
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        ...base(),
        http.put(
          "*/api/v1/contracts/templates/t-1/draft",
          async ({ request }) => {
            sent.push({
              url: new URL(request.url),
              type: request.headers.get("Content-Type"),
              body: await request.text(),
            });
            return HttpResponse.json({
              template: template(),
              structural_validation: {
                valid: false,
                error_count: 2,
                errors: [
                  { pointer: "/id", message: "id is required" },
                  { pointer: "", message: "kind is wrong" },
                ],
              },
            });
          },
        ),
      ],
    });
    const editor = await screen.findByLabelText(
      "Draft document (YAML or JSON)",
    );
    const save = await screen.findByRole("button", { name: "Save draft" });
    const publish = await screen.findByRole("button", {
      name: "Publish template",
    });
    // Nothing changed yet: nothing to save, publishing allowed.
    expect(save).toBeDisabled();
    expect(publish).toBeEnabled();
    expect(screen.queryByText("Save the draft before publishing.")).toBeNull();

    await user.clear(editor);
    await user.type(editor, '{{"kind": "DataContract"}');
    const name = screen.getByLabelText("Name");
    await user.clear(name);
    await user.type(name, "  Renamed table  ");
    expect(save).toBeEnabled();
    expect(publish).toBeDisabled();
    expect(screen.getByText("Save the draft before publishing.")).toBeVisible();

    await user.click(save);
    expect(
      await screen.findByText("The draft is saved, with schema problems"),
    ).toBeVisible();
    expect(sent).toHaveLength(1);
    expect(sent[0]?.type).toBe("application/json");
    expect(sent[0]?.body).toBe('{"kind": "DataContract"}');
    expect(sent[0]?.url.searchParams.get("name")).toBe("Renamed table");
    expect(
      screen.getByText("/id", { selector: "code" }).closest("li"),
    ).toHaveTextContent("/id: id is required");
    // An empty pointer is the document root, shown as "/".
    expect(
      screen.getByText("/", { selector: "code" }).closest("li"),
    ).toHaveTextContent("/: kind is wrong");
  });

  it("saves a YAML draft without a name and confirms a valid schema", async () => {
    const user = userEvent.setup();
    const sent: { url: URL; type: string | null }[] = [];
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        ...base(),
        http.put("*/api/v1/contracts/templates/t-1/draft", ({ request }) => {
          sent.push({
            url: new URL(request.url),
            type: request.headers.get("Content-Type"),
          });
          return HttpResponse.json({
            template: template(),
            structural_validation: { valid: true, error_count: 0, errors: [] },
          });
        }),
      ],
    });
    const editor = await screen.findByLabelText(
      "Draft document (YAML or JSON)",
    );
    // Editing needs the capabilities: wait until the draft is editable.
    await screen.findByRole("button", { name: "Save draft" });
    await user.type(editor, "status: draft\n");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(
      await screen.findByText("The draft is saved and passes the ODCS schema."),
    ).toBeVisible();
    expect(sent[0]?.type).toBe("application/yaml");
    expect(sent[0]?.url.searchParams.has("name")).toBe(false);
  });

  it("keeps the edit and shows the problem when saving fails", async () => {
    const user = userEvent.setup();
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        ...base(),
        http.put("*/api/v1/contracts/templates/t-1/draft", () =>
          problem(409, "Draft conflict"),
        ),
      ],
    });
    const editor = await screen.findByLabelText(
      "Draft document (YAML or JSON)",
    );
    // Editing needs the capabilities: wait until the draft is editable.
    await screen.findByRole("button", { name: "Save draft" });
    await user.type(editor, "x: 1");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText("Draft conflict")).toBeVisible();
    expect(editor).toHaveValue(`${DOC}x: 1`);
  });

  it("keeps the confirm open with the problem when publishing fails, and goes back", async () => {
    const user = userEvent.setup();
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        ...base(),
        http.post("*/api/v1/contracts/templates/t-1/publish", () =>
          problem(422, "Draft does not validate"),
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Publish template" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Publish template version 2?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Publish template" }),
    );
    expect(
      await within(confirm).findByText("Draft does not validate"),
    ).toBeVisible();
    await user.click(within(confirm).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(screen.queryByText(/is published/)).toBeNull();
  });
});

describe("template versions tab", () => {
  it("shows the empty state", async () => {
    const user = userEvent.setup();
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: base({
        tpl: template({ publish_counter: 0, latest_version_no: null }),
        versions: () => HttpResponse.json({ items: [] }),
      }),
    });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    expect(
      await screen.findByRole("heading", { name: "No published versions" }),
    ).toBeVisible();
  });

  it("shows a problem for the versions and retries", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: base({
        versions: () => {
          calls += 1;
          return calls === 1
            ? problem(500, "Versions unavailable")
            : HttpResponse.json({ items: [version(1)] });
        },
      }),
    });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    expect(await screen.findByText("Versions unavailable")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("button", { name: "View version 1" }),
    ).toBeVisible();
  });

  it("lists versions newest first and marks deprecated ones", async () => {
    const user = userEvent.setup();
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: base({
        versions: () =>
          HttpResponse.json({ items: [version(1, true), version(2)] }),
      }),
    });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    await screen.findByRole("button", { name: "View version 2" });
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Version 2");
    expect(rows[0]).toHaveTextContent("Batch table v2");
    expect(rows[0]).toHaveTextContent("v3.1.0");
    expect(rows[0]).toHaveTextContent("by local:admin");
    expect(within(rows[0] as HTMLElement).getByText("Offered")).toBeVisible();
    expect(
      within(rows[1] as HTMLElement).getByText("Deprecated"),
    ).toBeVisible();
    expect(
      await screen.findByRole("button", { name: "Deprecate version 2" }),
    ).toBeVisible();
    // A deprecated version cannot be deprecated again.
    expect(
      screen.queryByRole("button", { name: "Deprecate version 1" }),
    ).toBeNull();
  });

  it("opens a published version read-only", async () => {
    const user = userEvent.setup();
    const asked: string[] = [];
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        ...base(),
        http.get(
          "*/api/v1/contracts/templates/t-1/versions/:n/document",
          ({ params }) => {
            asked.push(String(params.n));
            return HttpResponse.text("kind: DataContract\nversion: 1.0.0\n");
          },
        ),
      ],
    });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    await user.click(
      await screen.findByRole("button", { name: "View version 1" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Version 1 of Batch table",
    });
    expect(
      within(dialog).getByText("Read-only. Published versions never change."),
    ).toBeVisible();
    expect(await within(dialog).findByText(/version: 1\.0\.0/)).toBeVisible();
    expect(asked).toEqual(["1"]);
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("shows a problem when a version document cannot load", async () => {
    const user = userEvent.setup();
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        ...base(),
        http.get("*/api/v1/contracts/templates/t-1/versions/1/document", () =>
          problem(500, "Version document unavailable"),
        ),
      ],
    });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    await user.click(
      await screen.findByRole("button", { name: "View version 1" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Version 1 of Batch table",
    });
    expect(
      await within(dialog).findByText("Version document unavailable"),
    ).toBeVisible();
  });

  it("shows an empty version document body as empty text", async () => {
    const user = userEvent.setup();
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        ...base(),
        http.get(
          "*/api/v1/contracts/templates/t-1/versions/1/document",
          () => new HttpResponse(null, { status: 204 }),
        ),
      ],
    });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    await user.click(
      await screen.findByRole("button", { name: "View version 1" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Version 1 of Batch table",
    });
    await waitFor(() => {
      expect(dialog.querySelector("pre")).not.toBeNull();
    });
    expect(dialog.querySelector("pre")?.textContent).toBe("");
  });

  it("keeps the deprecate confirm open with the problem, then keeps the version", async () => {
    const user = userEvent.setup();
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        ...base(),
        http.post("*/api/v1/contracts/templates/t-1/versions/1/deprecate", () =>
          problem(409, "Version is pinned"),
        ),
      ],
    });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    await user.click(
      await screen.findByRole("button", { name: "Deprecate version 1" }),
    );
    const confirm = await screen.findByRole("alertdialog", {
      name: "Deprecate template version 1?",
    });
    await user.click(
      within(confirm).getByRole("button", { name: "Deprecate version" }),
    );
    expect(await within(confirm).findByText("Version is pinned")).toBeVisible();
    await user.click(
      within(confirm).getByRole("button", { name: "Keep version 1" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });
});

describe("template responses without a body", () => {
  it("shows the generic problem when the template answers 204", async () => {
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        http.get(
          "*/api/v1/contracts/templates/t-1",
          () => new HttpResponse(null, { status: 204 }),
        ),
      ],
    });
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });

  it("shows the generic problem when the versions answer 204", async () => {
    const user = userEvent.setup();
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: base({
        versions: () => new HttpResponse(null, { status: 204 }),
      }),
    });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });

  it("shows the generic problem when save, publish and deprecate answer 204", async () => {
    const user = userEvent.setup();
    const empty = () => new HttpResponse(null, { status: 204 });
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: [
        ...base(),
        http.put("*/api/v1/contracts/templates/t-1/draft", empty),
        http.post("*/api/v1/contracts/templates/t-1/publish", empty),
        http.post(
          "*/api/v1/contracts/templates/t-1/versions/1/deprecate",
          empty,
        ),
      ],
    });
    const editor = await screen.findByLabelText(
      "Draft document (YAML or JSON)",
    );
    // Editing needs the capabilities: wait until the draft is editable.
    await screen.findByRole("button", { name: "Save draft" });
    await user.type(editor, "a: 1");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText("Something went wrong")).toBeVisible();

    await user.clear(editor);
    await user.type(editor, DOC);
    await user.click(screen.getByRole("button", { name: "Publish template" }));
    const publish = await screen.findByRole("alertdialog");
    await user.click(
      within(publish).getByRole("button", { name: "Publish template" }),
    );
    expect(
      await within(publish).findByText(
        "The action did not complete. Try again.",
      ),
    ).toBeVisible();
    await user.click(within(publish).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });

    await user.click(screen.getByRole("tab", { name: "Versions" }));
    await user.click(
      await screen.findByRole("button", { name: "Deprecate version 1" }),
    );
    const deprecate = await screen.findByRole("alertdialog");
    await user.click(
      within(deprecate).getByRole("button", { name: "Deprecate version" }),
    );
    expect(
      await within(deprecate).findByText(
        "The action did not complete. Try again.",
      ),
    ).toBeVisible();
  });
});

describe("template versions sorting", () => {
  it("sorts the versions by number", async () => {
    const user = userEvent.setup();
    renderApp("/templates/t-1", {
      folders,
      permissions: ALL,
      handlers: base({
        versions: () =>
          HttpResponse.json({ items: [version(1), version(2), version(3)] }),
      }),
    });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    await screen.findByRole("button", { name: "View version 3" });
    const order = () =>
      screen.getAllByRole("rowheader").map((cell) => cell.textContent);
    expect(order()).toEqual(["Version 3", "Version 2", "Version 1"]);
    await user.click(
      within(screen.getByRole("table")).getByRole("button", {
        name: /^Version/,
      }),
    );
    expect(order()).toEqual(["Version 1", "Version 2", "Version 3"]);
  });
});
