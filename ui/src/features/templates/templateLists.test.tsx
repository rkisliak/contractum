// /templates, the shared templates table, the create dialog and the folder Templates tab through
// the real router, beyond templates.test.tsx: capability gating, the folder filter, pagination,
// list problems, form validation and API failure, resetting the dialog, the "Use in new
// contract" action on every folder kind, archived folders and the available-templates states.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AvailableTemplate, Template } from "@/api/types";
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
const ledger = makeFolder({
  id: "ledger-id",
  parent_id: "acme-id",
  kind: "team",
  slug: "ledger",
  display_name: "Ledger",
  path: "acme.ledger",
});
const folders = [rootFolder, acme, ledger];
const ALL = [
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

function available(
  overrides: Partial<AvailableTemplate> = {},
): AvailableTemplate {
  return {
    template_id: "t-2",
    name: "Inherited table",
    folder_id: "acme-id",
    latest_version_no: 3,
    version_nos: [2, 3],
    ...overrides,
  };
}

const problem = (status: number, title: string) =>
  HttpResponse.json(
    { type: "urn:dc:problem:test", title, status },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );

const noBody = () => new HttpResponse(null, { status: 204 });

describe("templates list page", () => {
  it("hides Create template without template:create and pages with a cursor", async () => {
    const user = userEvent.setup();
    const seen: URLSearchParams[] = [];
    renderApp("/templates", {
      folders,
      permissions: ["template:read"],
      handlers: [
        http.get("*/api/v1/contracts/templates", ({ request }) => {
          const params = new URL(request.url).searchParams;
          seen.push(params);
          return HttpResponse.json(
            params.get("cursor") === null
              ? {
                  items: [
                    template({
                      latest_version_no: null,
                      publish_counter: 0,
                    }),
                  ],
                  next_cursor: "c-2",
                }
              : {
                  items: [template({ id: "t-9", name: "Second page table" })],
                  next_cursor: null,
                },
          );
        }),
      ],
    });
    const link = await screen.findByRole("link", { name: "Batch table" });
    const row = link.closest("tr") as HTMLElement;
    expect(within(row).getByText("Draft only")).toBeVisible();
    expect(within(row).getByText("0")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Create template" }),
    ).toBeNull();
    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(
      await screen.findByRole("link", { name: "Second page table" }),
    ).toHaveAttribute("href", "/templates/t-9");
    expect(Object.fromEntries(seen[0]?.entries() ?? [])).toEqual({
      limit: "50",
    });
    expect(Object.fromEntries(seen[1]?.entries() ?? [])).toEqual({
      limit: "50",
      cursor: "c-2",
    });
  });

  it("narrows the list to one owning folder and back", async () => {
    const user = userEvent.setup();
    const seen: (string | null)[] = [];
    const { router } = renderApp("/templates", {
      folders,
      permissions: ALL,
      handlers: [
        http.get("*/api/v1/contracts/templates", ({ request }) => {
          const folder = new URL(request.url).searchParams.get("folder_id");
          seen.push(folder);
          return HttpResponse.json({ items: [], next_cursor: null });
        }),
      ],
    });
    expect(
      await screen.findByText(
        "Create a template at an organization, project or team so new contracts start from it.",
      ),
    ).toBeVisible();
    const select = screen.getByLabelText("Owning folder");
    await waitFor(() => {
      expect(select).toBeEnabled();
    });
    await user.selectOptions(select, "acme-id");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ folder_id: "acme-id" });
    });
    expect(
      await screen.findByText(/No template is owned by this folder/),
    ).toBeVisible();
    expect(seen).toContain("acme-id");
    await user.selectOptions(select, "");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    expect(seen[0]).toBeNull();
  });

  it("offers exactly one Create template button when empty", async () => {
    const user = userEvent.setup();
    renderApp("/templates", {
      folders,
      permissions: ALL,
      handlers: [
        http.get("*/api/v1/contracts/templates", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
      ],
    });
    await screen.findByRole("heading", { name: "No templates yet" });
    const buttons = await screen.findAllByRole("button", {
      name: "Create template",
    });
    expect(buttons).toHaveLength(1);
    await user.click(buttons[0] as HTMLElement);
    expect(
      await screen.findByRole("dialog", { name: "Create template" }),
    ).toBeVisible();
  });

  it("shows a list problem and retries", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/templates", {
      folders,
      permissions: ALL,
      handlers: [
        http.get("*/api/v1/contracts/templates", () => {
          calls += 1;
          return calls === 1
            ? problem(500, "Template list failed")
            : HttpResponse.json({ items: [template()], next_cursor: null });
        }),
      ],
    });
    expect(await screen.findByText("Template list failed")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("link", { name: "Batch table" }),
    ).toBeVisible();
  });

  it("shows the generic problem for a list answered without a body", async () => {
    renderApp("/templates", {
      folders,
      permissions: ALL,
      handlers: [http.get("*/api/v1/contracts/templates", noBody)],
    });
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });
});

describe("create template dialog", () => {
  function emptyList() {
    return http.get("*/api/v1/contracts/templates", () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    );
  }

  it("validates the name and folder before sending anything", async () => {
    const user = userEvent.setup();
    let posts = 0;
    renderApp("/templates", {
      folders,
      permissions: ALL,
      handlers: [
        emptyList(),
        http.post("*/api/v1/contracts/folders/:id/templates", () => {
          posts += 1;
          return problem(500, "unexpected");
        }),
      ],
    });
    await screen.findByRole("heading", { name: "No templates yet" });
    await user.click(
      (
        await screen.findAllByRole("button", {
          name: "Create template",
        })
      )[0] as HTMLElement,
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Create template",
    });
    await user.type(within(dialog).getByLabelText("Name"), "   ");
    await user.click(
      within(dialog).getByRole("button", { name: "Create template" }),
    );
    expect(
      await within(dialog).findByText("Enter a name for the template."),
    ).toBeVisible();
    expect(
      within(dialog).getByText("Choose the folder that owns it."),
    ).toBeVisible();
    expect(posts).toBe(0);
  });

  it("sends a JSON document to the chosen folder and shows the API problem", async () => {
    const user = userEvent.setup();
    const sent: { url: URL; type: string | null; body: string }[] = [];
    renderApp("/templates", {
      folders,
      permissions: ALL,
      handlers: [
        emptyList(),
        http.post(
          "*/api/v1/contracts/folders/:id/templates",
          async ({ request }) => {
            sent.push({
              url: new URL(request.url),
              type: request.headers.get("Content-Type"),
              body: await request.text(),
            });
            return problem(422, "Document is not ODCS");
          },
        ),
      ],
    });
    await screen.findByRole("heading", { name: "No templates yet" });
    await user.click(
      (
        await screen.findAllByRole("button", {
          name: "Create template",
        })
      )[0] as HTMLElement,
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Create template",
    });
    await user.type(within(dialog).getByLabelText("Name"), " Orders ");
    await user.selectOptions(
      within(dialog).getByLabelText("Owning folder"),
      "ledger-id",
    );
    const doc = within(dialog).getByLabelText("First draft (YAML or JSON)");
    expect((doc as HTMLTextAreaElement).value).toContain(
      "id: urn:example:template:replace-me",
    );
    await user.clear(doc);
    await user.type(doc, '  {{"kind": "DataContract"}');
    await user.click(
      within(dialog).getByRole("button", { name: "Create template" }),
    );
    expect(
      await within(dialog).findByText("Document is not ODCS"),
    ).toBeVisible();
    expect(sent).toHaveLength(1);
    expect(sent[0]?.url.pathname).toBe(
      "/api/v1/contracts/folders/ledger-id/templates",
    );
    expect(sent[0]?.url.searchParams.get("name")).toBe("Orders");
    expect(sent[0]?.type).toBe("application/json");
    expect(sent[0]?.body).toBe('  {"kind": "DataContract"}');
  });

  it("shows the generic problem when create answers without a body", async () => {
    const user = userEvent.setup();
    renderApp("/templates?folder_id=acme-id", {
      folders,
      permissions: ALL,
      handlers: [
        emptyList(),
        http.post("*/api/v1/contracts/folders/acme-id/templates", noBody),
      ],
    });
    await screen.findByRole("heading", { name: "No templates yet" });
    await user.click(
      (
        await screen.findAllByRole("button", {
          name: "Create template",
        })
      )[0] as HTMLElement,
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Create template",
    });
    await user.type(within(dialog).getByLabelText("Name"), "Orders");
    await user.click(
      within(dialog).getByRole("button", { name: "Create template" }),
    );
    expect(
      await within(dialog).findByText("Something went wrong"),
    ).toBeVisible();
  });

  it("starts fresh after Go back and after Escape", async () => {
    const user = userEvent.setup();
    renderApp("/templates?folder_id=acme-id", {
      folders,
      permissions: ALL,
      handlers: [emptyList()],
    });
    await screen.findByRole("heading", { name: "No templates yet" });
    const open = async () => {
      await user.click(
        (
          await screen.findAllByRole("button", {
            name: "Create template",
          })
        )[0] as HTMLElement,
      );
      return screen.findByRole("dialog", { name: "Create template" });
    };
    let dialog = await open();
    // The page's folder is preselected.
    expect(within(dialog).getByLabelText("Owning folder")).toHaveValue(
      "acme-id",
    );
    await user.type(within(dialog).getByLabelText("Name"), "Draft name");
    await user.selectOptions(
      within(dialog).getByLabelText("Owning folder"),
      "ledger-id",
    );
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    dialog = await open();
    expect(within(dialog).getByLabelText("Name")).toHaveValue("");
    expect(within(dialog).getByLabelText("Owning folder")).toHaveValue(
      "acme-id",
    );
    await user.type(within(dialog).getByLabelText("Name"), "Another");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    dialog = await open();
    expect(within(dialog).getByLabelText("Name")).toHaveValue("");
  });
});

describe("folder templates tab", () => {
  it("offers Use in new contract on a Team folder and creates a template there", async () => {
    const user = userEvent.setup();
    const seen: URLSearchParams[] = [];
    renderApp("/folders/ledger-id/templates", {
      folders,
      permissions: [...ALL, "contract:create"],
      handlers: [
        http.get("*/api/v1/contracts/templates", ({ request }) => {
          expect(new URL(request.url).searchParams.get("folder_id")).toBe(
            "ledger-id",
          );
          return HttpResponse.json({ items: [], next_cursor: null });
        }),
        http.get(
          "*/api/v1/contracts/folders/ledger-id/available-templates",
          ({ request }) => {
            const params = new URL(request.url).searchParams;
            seen.push(params);
            return HttpResponse.json(
              params.get("cursor") === null
                ? { items: [available()], next_cursor: "a-2" }
                : {
                    items: [
                      available({
                        template_id: "t-5",
                        name: "Platform table",
                        latest_version_no: 1,
                        version_nos: [1],
                      }),
                    ],
                    next_cursor: null,
                  },
            );
          },
        ),
      ],
    });
    const use = await screen.findByRole("link", {
      name: "Use in new contract: Inherited table",
    });
    expect(use).toHaveAttribute(
      "href",
      "/contracts/new?folder=ledger-id&template=t-2",
    );
    const row = use.closest("tr") as HTMLElement;
    expect(within(row).getByText("Version 3")).toBeVisible();
    expect(within(row).getByText("2, 3")).toBeVisible();
    expect(screen.getByText(/This folder owns no template/)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(
      await screen.findByRole("link", {
        name: "Use in new contract: Platform table",
      }),
    ).toBeVisible();
    expect(seen[1]?.get("cursor")).toBe("a-2");
    expect(seen[0]?.get("limit")).toBe("50");

    await user.click(screen.getByRole("button", { name: "Create template" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Create template",
    });
    expect(within(dialog).getByLabelText("Owning folder")).toHaveValue(
      "ledger-id",
    );
  });

  it("offers neither creating nor starting contracts in an archived folder", async () => {
    const archived = makeFolder({
      ...ledger,
      id: "old-id",
      slug: "old",
      display_name: "Old team",
      path: "acme.old",
      archived: true,
      archived_at: "2026-11-05T10:00:00Z",
    });
    renderApp("/folders/old-id/templates", {
      folders: [...folders, archived],
      permissions: [...ALL, "contract:create"],
      handlers: [
        http.get("*/api/v1/contracts/templates", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
        http.get("*/api/v1/contracts/folders/old-id/available-templates", () =>
          HttpResponse.json({ items: [available()], next_cursor: null }),
        ),
      ],
    });
    expect(
      await screen.findByRole("link", { name: "Inherited table" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Create template" }),
    ).toBeNull();
    expect(
      screen.queryByRole("link", { name: /Use in new contract/ }),
    ).toBeNull();
  });

  it("explains that starting contracts is not included without contract:create", async () => {
    renderApp("/folders/acme-id/templates", {
      folders,
      permissions: ["template:read"],
      handlers: [
        http.get("*/api/v1/contracts/templates", () =>
          HttpResponse.json({ items: [template()], next_cursor: null }),
        ),
      ],
    });
    expect(
      await screen.findByRole("heading", {
        level: 3,
        name: "Not available at your access level",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(/Starting contracts from templates is not included/),
    ).toBeVisible();
    expect(
      await screen.findByRole("link", { name: "Batch table" }),
    ).toBeVisible();
    // template:read alone does not allow creating.
    expect(
      screen.queryByRole("button", { name: "Create template" }),
    ).toBeNull();
  });

  it("shows the available-templates problem, retries, and the empty state", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/folders/acme-id/templates", {
      folders,
      permissions: ["contract:create"],
      handlers: [
        http.get(
          "*/api/v1/contracts/folders/acme-id/available-templates",
          () => {
            calls += 1;
            return calls === 1
              ? problem(500, "Available list failed")
              : HttpResponse.json({ items: [], next_cursor: null });
          },
        ),
      ],
    });
    expect(await screen.findByText("Available list failed")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", { name: "No templates available" }),
    ).toBeVisible();
  });

  it("offers Use in new contract on an organization folder with contract:create", async () => {
    renderApp("/folders/acme-id/templates", {
      folders,
      permissions: ["contract:create"],
      handlers: [
        http.get("*/api/v1/contracts/folders/acme-id/available-templates", () =>
          HttpResponse.json({ items: [available()], next_cursor: null }),
        ),
      ],
    });
    expect(
      await screen.findByRole("link", { name: "Inherited table" }),
    ).toHaveAttribute("href", "/templates/t-2");
    expect(
      await screen.findByRole("link", {
        name: "Use in new contract: Inherited table",
      }),
    ).toHaveAttribute("href", "/contracts/new?folder=acme-id&template=t-2");
  });

  it("shows the generic problem for available templates answered without a body", async () => {
    renderApp("/folders/acme-id/templates", {
      folders,
      permissions: ["contract:create"],
      handlers: [
        http.get(
          "*/api/v1/contracts/folders/acme-id/available-templates",
          noBody,
        ),
      ],
    });
    expect(await screen.findByText("Something went wrong")).toBeVisible();
  });
});

describe("client-side sorting", () => {
  const names = () =>
    screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("link")[0]?.textContent);

  it("sorts the templates table by name, published versions and update time", async () => {
    const user = userEvent.setup();
    renderApp("/templates", {
      folders,
      permissions: ALL,
      handlers: [
        http.get("*/api/v1/contracts/templates", () =>
          HttpResponse.json({
            items: [
              template({
                id: "t-b",
                name: "Bravo",
                publish_counter: 1,
                updated_at: "2026-11-03T10:00:00Z",
              }),
              template({
                id: "t-a",
                name: "Alpha",
                publish_counter: 5,
                updated_at: "2026-11-01T10:00:00Z",
              }),
              template({
                id: "t-c",
                name: "Charlie",
                publish_counter: 3,
                updated_at: "2026-11-02T10:00:00Z",
              }),
            ],
            next_cursor: null,
          }),
        ),
      ],
    });
    await screen.findByRole("link", { name: "Alpha" });
    expect(names()).toEqual(["Bravo", "Alpha", "Charlie"]);
    await user.click(screen.getByRole("button", { name: /^Name/ }));
    expect(names()).toEqual(["Alpha", "Bravo", "Charlie"]);
    await user.click(
      screen.getByRole("button", { name: /^Published versions/ }),
    );
    expect(names()).toEqual(["Bravo", "Charlie", "Alpha"]);
    await user.click(screen.getByRole("button", { name: /^Updated/ }));
    expect(names()).toEqual(["Alpha", "Charlie", "Bravo"]);
  });

  it("sorts the available templates by name", async () => {
    const user = userEvent.setup();
    renderApp("/folders/acme-id/templates", {
      folders,
      permissions: ["contract:create"],
      handlers: [
        http.get("*/api/v1/contracts/folders/acme-id/available-templates", () =>
          HttpResponse.json({
            items: [
              available({ template_id: "t-z", name: "Zulu" }),
              available({ template_id: "t-y", name: "Yankee" }),
            ],
            next_cursor: null,
          }),
        ),
      ],
    });
    await screen.findByRole("link", { name: "Zulu" });
    expect(names()).toEqual(["Zulu", "Yankee"]);
    await user.click(screen.getByRole("button", { name: /^Name/ }));
    expect(names()).toEqual(["Yankee", "Zulu"]);
  });
});
