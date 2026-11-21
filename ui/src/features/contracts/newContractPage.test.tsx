// /contracts/new through the real router, the sources the starter test does not walk: a blank
// YAML or JSON document (the platform's default ODCS version, edits sent byte for byte), a
// template with a pinned version and a name override, a version or draft of another contract, an
// uploaded file (size limit, media type from the extension), every form-level refusal, a create
// failure, Go back, and the page for a caller who may create contracts nowhere.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import {
  CONTRACT_ALL,
  CONTRACT_API,
  CONTRACT_DOC,
  CONTRACT_READER,
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
import { MAX_UPLOAD_BYTES } from "./NewContractPage";

const FOLDER_API = "*/api/v1/contracts/folders/ledger-id";

const created = {
  contract: makeContract(),
  version: { number: 1, state: "draft" },
  draft: { id: "d-1", revision: 1, etag: '"r1"' },
  structural_validation: { valid: true, error_count: 0, errors: [] },
  validation: { status: "unavailable" },
};

type Captured = { url: string; contentType: string | null; body: string };

/** Answers every create route with `created` and records what was sent. */
function captureCreates(sent: Captured[]): RequestHandler {
  return http.post(`${FOLDER_API}/contracts*`, async ({ request }) => {
    sent.push({
      url: new URL(request.url).pathname,
      contentType: request.headers.get("Content-Type"),
      body: await request.text(),
    });
    return HttpResponse.json(created, { status: 201 });
  });
}

function newContract(
  path: string,
  handlers: RequestHandler[] = [],
  permissions = CONTRACT_ALL,
) {
  return renderApp(path, {
    folders: contractFolders,
    permissions,
    // The created contract's draft editor, where a successful create lands.
    handlers: [
      ...handlers,
      ...contractHandlers({ drafts: [makeDraft("d-1")] }),
      http.get(`${CONTRACT_API}/drafts/d-1`, () =>
        HttpResponse.json(makeDraft("d-1")),
      ),
      http.get(`${CONTRACT_API}/drafts/d-1/document`, () =>
        textDocument(CONTRACT_DOC, "application/yaml", '"d-1-r1"'),
      ),
    ],
  });
}

async function chooseSource(label: string) {
  const user = userEvent.setup();
  // A source's accessible name is its label followed by its help line.
  await user.click(
    await screen.findByRole("radio", { name: new RegExp(`^${label}`) }),
  );
  return user;
}

async function submitAndExpectDraft(
  user: ReturnType<typeof userEvent.setup>,
  router: { state: { location: { pathname: string } } },
) {
  await user.click(screen.getByRole("button", { name: "Create contract" }));
  await waitFor(() => {
    expect(router.state.location.pathname).toBe("/contracts/c-1/drafts/d-1");
  });
}

afterEach(() => {
  tokenStore.clear();
});

describe("new contract access and refusals", () => {
  it("is not available to a caller who may create contracts nowhere", async () => {
    newContract("/contracts/new", [], CONTRACT_READER);
    expect(
      await screen.findByText("Not available at your access level"),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Create contract" }),
    ).toBeNull();
  });

  it("asks for a folder, a name and an ODCS id before sending anything", async () => {
    const sent: Captured[] = [];
    const user = userEvent.setup();
    newContract("/contracts/new", [captureCreates(sent)]);
    await user.click(
      await screen.findByRole("button", { name: "Create contract" }),
    );
    expect(await screen.findByText("Choose a folder.")).toBeVisible();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Folder" }),
      "ledger-id",
    );
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    expect(await screen.findByText("Enter a name.")).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Orders");
    await user.clear(screen.getByRole("textbox", { name: /ODCS id/ }));
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    expect(await screen.findByText("Enter an ODCS id.")).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Name" }), " 😀");
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    expect(
      await screen.findByText("Fix the name before creating the contract."),
    ).toBeVisible();
    expect(sent).toEqual([]);
  });

  it("asks for the source's own choice: a template, a source ref or a file", async () => {
    newContract("/contracts/new?folder=ledger-id", [
      http.get(`${FOLDER_API}/available-templates`, () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
      http.get("*/api/v1/contracts/contracts", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    ]);
    const user = await chooseSource("From a template");
    expect(
      await screen.findByText(
        "No published templates are available at this folder.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    expect(await screen.findByText("Choose a template.")).toBeVisible();

    await user.click(
      screen.getByRole("radio", { name: /^From another contract/ }),
    );
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    expect(
      await screen.findByText(
        "Choose a source contract and a version or draft.",
      ),
    ).toBeVisible();

    await user.click(screen.getByRole("radio", { name: /^Upload a file/ }));
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    expect(await screen.findByText("Choose a file to upload.")).toBeVisible();
  });

  it("shows a create refusal and goes back to the list", async () => {
    const user = userEvent.setup();
    const { router } = newContract("/contracts/new?folder=ledger-id", [
      http.post(`${FOLDER_API}/contracts/starter`, () =>
        problemResponse(409, "odcs-id-taken"),
      ),
    ]);
    await user.type(
      await screen.findByRole("textbox", { name: "Name" }),
      "Orders",
    );
    await user.click(screen.getByRole("button", { name: "Create contract" }));
    expect(await screen.findByText("Title of odcs-id-taken")).toBeVisible();
    expect(router.state.location.pathname).toBe("/contracts/new");
    await user.click(screen.getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts");
    });
  });
});

describe("new contract from a blank document", () => {
  it("starts YAML with the platform default ODCS version and sends the edited bytes", async () => {
    const sent: Captured[] = [];
    const settings = {
      banner: null,
      entra_default_tenant_id: null,
      entra_default_owner_folder_id: null,
      default_odcs_api_version: "v3.1.0",
      require_lifecycle_reason: false,
      credential_max_ttl_days: 365,
      password_rules: {
        min_length: 12,
        require_upper: false,
        require_lower: false,
        require_digit: false,
        require_symbol: false,
      },
    };
    const { router } = newContract("/contracts/new?folder=ledger-id", [
      captureCreates(sent),
      http.get("*/api/v1/identity/settings/public", () =>
        HttpResponse.json(settings),
      ),
    ]);
    const user = await chooseSource("Blank document");
    const editor = screen.getByRole("textbox", { name: "Contract document" });
    await waitFor(() => {
      expect(editor).toHaveValue(
        'apiVersion: v3.1.0\nkind: DataContract\nid: urn:acme:payments:ledger:contract\nname: New contract\nversion: "0.1.0"\nstatus: draft\n',
      );
    });
    // No name or ODCS id fields: the document carries both.
    expect(screen.queryByRole("textbox", { name: "Name" })).toBeNull();
    await user.type(editor, "domain: sales\n");
    await submitAndExpectDraft(user, router);
    expect(sent).toHaveLength(1);
    expect(sent[0]?.url).toBe("/api/v1/contracts/folders/ledger-id/contracts");
    expect(sent[0]?.contentType).toBe("application/yaml");
    expect(sent[0]?.body).toMatch(/status: draft\ndomain: sales\n$/);
  });

  it("switches to a JSON skeleton and sends it as JSON", async () => {
    const sent: Captured[] = [];
    const { router } = newContract("/contracts/new?folder=ledger-id", [
      captureCreates(sent),
    ]);
    const user = await chooseSource("Blank document");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Format" }),
      "json",
    );
    const editor = screen.getByRole<HTMLTextAreaElement>("textbox", {
      name: "Contract document",
    });
    expect(JSON.parse(editor.value)).toEqual({
      apiVersion: "v3.2.0",
      kind: "DataContract",
      id: "urn:acme:payments:ledger:contract",
      name: "New contract",
      version: "0.1.0",
      status: "draft",
    });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Format" }),
      "yaml",
    );
    expect(editor.value).toMatch(/^apiVersion: v3\.2\.0\n/);
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Format" }),
      "json",
    );
    await submitAndExpectDraft(user, router);
    expect(sent[0]?.contentType).toBe("application/json");
    expect(JSON.parse(sent[0]?.body ?? "{}")).toMatchObject({
      kind: "DataContract",
    });
  });
});

describe("new contract from a template", () => {
  const template = (id: string, name: string, versions: number[]) => ({
    template_id: id,
    name,
    folder_id: "ledger-id",
    latest_version_no: versions.at(-1),
    version_nos: versions,
  });

  it("previews the chosen version and sends the pin and the name override", async () => {
    const sent: Captured[] = [];
    const previews: string[] = [];
    const { router } = newContract("/contracts/new?folder=ledger-id", [
      captureCreates(sent),
      http.get(`${FOLDER_API}/available-templates`, () =>
        HttpResponse.json({
          items: [
            template("t-1", "Batch table", [1, 2]),
            template("t-2", "Stream", [1]),
          ],
          next_cursor: null,
        }),
      ),
      http.get(
        `${FOLDER_API}/available-templates/:id/versions/:no/document`,
        ({ params }) => {
          previews.push(`${String(params.id)}@${String(params.no)}`);
          return new HttpResponse(
            `kind: DataContract # v${String(params.no)}\n`,
            {
              headers: { "Content-Type": "application/yaml" },
            },
          );
        },
      ),
    ]);
    const user = await chooseSource("From a template");
    const version = screen.getByRole("combobox", { name: "Template version" });
    expect(version).toBeDisabled();
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Template" }),
      "t-1",
    );
    expect(
      await screen.findByRole("textbox", { name: "Template preview" }),
    ).toHaveValue("kind: DataContract # v2\n");
    expect(
      within(version)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Latest", "Version 1", "Version 2"]);
    await user.selectOptions(version, "1");
    await waitFor(() => {
      expect(
        screen.getByRole("textbox", { name: "Template preview" }),
      ).toHaveValue("kind: DataContract # v1\n");
    });
    expect(previews).toEqual(["t-1@2", "t-1@1"]);
    expect(
      screen.getByText("Optional: replaces the template's name."),
    ).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Orders");
    await submitAndExpectDraft(user, router);
    expect(sent[0]?.url).toBe(
      "/api/v1/contracts/folders/ledger-id/contracts/from-template",
    );
    expect(JSON.parse(sent[0]?.body ?? "{}")).toEqual({
      template_id: "t-1",
      odcs_id: "urn:acme:payments:ledger:orders",
      overrides: { name: "Orders" },
      template_version: 1,
    });
  });

  it("keeps the template's own name and latest version when none is given", async () => {
    const sent: Captured[] = [];
    const { router } = newContract(
      "/contracts/new?folder=ledger-id&template=t-2",
      [
        captureCreates(sent),
        http.get(`${FOLDER_API}/available-templates`, () =>
          HttpResponse.json({
            items: [template("t-2", "Stream", [1])],
            next_cursor: null,
          }),
        ),
        http.get(
          `${FOLDER_API}/available-templates/:id/versions/:no/document`,
          () => new HttpResponse(null, { status: 204 }),
        ),
      ],
    );
    const user = userEvent.setup();
    // An empty preview body still renders the (empty) preview.
    expect(
      await screen.findByRole("textbox", { name: "Template preview" }),
    ).toHaveValue("");
    await submitAndExpectDraft(user, router);
    expect(JSON.parse(sent[0]?.body ?? "{}")).toEqual({
      template_id: "t-2",
      odcs_id: "urn:acme:payments:ledger:contract",
    });
  });

  it("asks for a folder first, and shows a template list failure", async () => {
    newContract("/contracts/new?template=t-1", [
      http.get(`${FOLDER_API}/available-templates`, () =>
        problemResponse(500, "internal"),
      ),
    ]);
    expect(
      await screen.findByText("Choose a folder to see its templates."),
    ).toBeVisible();
    const user = userEvent.setup();
    await screen.findByRole("option", { name: "acme/payments/ledger" });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Folder" }),
      "ledger-id",
    );
    expect(await screen.findByText("Title of internal")).toBeVisible();
  });
});

describe("new contract from another contract", () => {
  const SOURCE = "*/api/v1/contracts/contracts/c-9";

  function sourceHandlers(queries: (string | null)[]): RequestHandler[] {
    return [
      http.get("*/api/v1/contracts/contracts", ({ request }) => {
        queries.push(new URL(request.url).searchParams.get("q"));
        return HttpResponse.json({
          items: [
            makeContract({
              id: "c-9",
              name: null,
              odcs_id: "urn:acme:billing",
            }),
          ],
          next_cursor: null,
        });
      }),
      http.get(`${SOURCE}/versions`, () =>
        HttpResponse.json({
          items: [
            makeVersion("2.0.0", { contract_id: "c-9" }),
            makeVersion("1.0.0", {
              contract_id: "c-9",
              lifecycle: "unpublished",
            }),
          ],
        }),
      ),
      http.get(`${SOURCE}/drafts`, () =>
        HttpResponse.json({
          items: [makeDraft("d-9", { declared_version: null, revision: 2 })],
        }),
      ),
    ];
  }

  it("searches contracts and copies a draft or a version", async () => {
    const sent: Captured[] = [];
    const queries: (string | null)[] = [];
    const { router } = newContract("/contracts/new?folder=ledger-id", [
      captureCreates(sent),
      ...sourceHandlers(queries),
    ]);
    const user = await chooseSource("From another contract");
    const ref = screen.getByRole("combobox", { name: "Version or draft" });
    expect(ref).toBeDisabled();
    expect(screen.queryByRole("textbox", { name: "Name" })).toBeNull();
    await user.type(
      screen.getByRole("searchbox", { name: "Find a contract" }),
      "bill",
    );
    await waitFor(() => {
      expect(queries).toContain("bill");
    });
    expect(queries[0]).toBeNull();
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Source contract" }),
      "c-9",
    );
    expect(
      screen.getByRole("option", {
        name: "urn:acme:billing (urn:acme:billing)",
      }),
    ).toBeInTheDocument();
    // Accessible names collapse the double space of a draft without a declared version.
    await screen.findByRole("option", { name: "Draft (revision 2)" });
    expect(
      within(ref)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([
      "Choose a version or draft",
      "Version 2.0.0",
      "Version 1.0.0 (unpublished)",
      "Draft  (revision 2)",
    ]);
    await user.selectOptions(ref, "d:d-9");
    await submitAndExpectDraft(user, router);
    expect(JSON.parse(sent[0]?.body ?? "{}")).toEqual({
      source_contract_id: "c-9",
      odcs_id: "urn:acme:payments:ledger:contract",
      source_draft_id: "d-9",
    });
  });

  it("copies a published version by its semver", async () => {
    const sent: Captured[] = [];
    const { router } = newContract("/contracts/new?folder=ledger-id", [
      captureCreates(sent),
      ...sourceHandlers([]),
    ]);
    const user = await chooseSource("From another contract");
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Source contract" }),
      "c-9",
    );
    await screen.findByRole("option", { name: "Version 2.0.0" });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Version or draft" }),
      "v:2.0.0",
    );
    await submitAndExpectDraft(user, router);
    expect(JSON.parse(sent[0]?.body ?? "{}")).toMatchObject({
      source_version: "2.0.0",
    });
  });
});

describe("new contract from an uploaded file", () => {
  it("refuses a file over 2 MiB, previews a JSON file and sends it as JSON", async () => {
    const sent: Captured[] = [];
    const { router } = newContract("/contracts/new?folder=ledger-id", [
      captureCreates(sent),
    ]);
    const user = await chooseSource("Upload a file");
    const input = screen.getByLabelText("Contract file");
    await user.upload(
      input,
      new File(["x".repeat(MAX_UPLOAD_BYTES + 1)], "big.yaml"),
    );
    expect(
      await screen.findByText(
        "The file is larger than 2 MiB. Upload a smaller document.",
      ),
    ).toBeVisible();
    await user.upload(
      input,
      new File(['{"kind":"DataContract"}'], "Orders.JSON", {
        type: "application/json",
      }),
    );
    expect(
      await screen.findByRole("textbox", { name: "Preview of Orders.JSON" }),
    ).toHaveValue('{"kind":"DataContract"}');
    expect(
      screen.queryByText(
        "The file is larger than 2 MiB. Upload a smaller document.",
      ),
    ).toBeNull();
    await submitAndExpectDraft(user, router);
    expect(sent[0]).toEqual({
      url: "/api/v1/contracts/folders/ledger-id/contracts",
      contentType: "application/json",
      body: '{"kind":"DataContract"}',
    });
  });

  it("sends a YAML file as YAML and forgets the file when the selection is cleared", async () => {
    const sent: Captured[] = [];
    const { router } = newContract("/contracts/new?folder=ledger-id", [
      captureCreates(sent),
    ]);
    const user = await chooseSource("Upload a file");
    const input = screen.getByLabelText<HTMLInputElement>("Contract file");
    await user.upload(input, new File(["kind: DataContract\n"], "orders.yml"));
    expect(
      await screen.findByRole("textbox", { name: "Preview of orders.yml" }),
    ).toBeVisible();
    await user.upload(input, []);
    await waitFor(() => {
      expect(
        screen.queryByRole("textbox", { name: "Preview of orders.yml" }),
      ).toBeNull();
    });
    await user.upload(input, new File(["kind: DataContract\n"], "orders.yml"));
    await screen.findByRole("textbox", { name: "Preview of orders.yml" });
    await submitAndExpectDraft(user, router);
    expect(sent[0]?.contentType).toBe("application/yaml");
    expect(sent[0]?.body).toBe("kind: DataContract\n");
  });
});
