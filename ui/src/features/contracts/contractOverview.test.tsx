// The contract frame and its first two tabs through the real router: the layout (a 404 is NotFound,
// another failure a ProblemAlert with retry, an unnamed contract reads as its ODCS id, a contract
// with nothing published and no read right has no Document tab), the Overview facts for every
// origin and unset value, the delete journey (success moves to the list, a pin or another refusal
// is explained), and the Document tab (no Latest yet, a failure with retry, downloads on the page
// and full screen). BlockedDelete and reasonsOf are also read directly for shapes the API may send.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RequestHandler } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { HttpProblem } from "@/api/problem";
import { tokenStore } from "@/auth/tokenStore";
import {
  CONTRACT_ALL,
  CONTRACT_API,
  CONTRACT_DOC,
  contractFolders,
  contractHandlers,
  makeContract,
  problemResponse,
  textDocument,
} from "@/test/contracts-fixtures";
import { http, HttpResponse } from "@/test/msw";
import { renderApp } from "@/test/renderApp";
import { BlockedDelete, reasonsOf } from "./ContractOverview";

afterEach(() => {
  tokenStore.clear();
  vi.restoreAllMocks();
});

function contractPage(
  path: string,
  {
    contract = makeContract(),
    permissions = CONTRACT_ALL,
    handlers = [],
  }: {
    contract?: Record<string, unknown>;
    permissions?: string[];
    handlers?: RequestHandler[];
  } = {},
) {
  return renderApp(path, {
    folders: contractFolders,
    permissions,
    handlers: [...handlers, ...contractHandlers({ contract })],
  });
}

function httpProblem(overrides: Partial<HttpProblem>): HttpProblem {
  return {
    kind: "problem",
    status: 409,
    slug: null,
    title: "Refused",
    detail: null,
    errors: [],
    requestId: null,
    ...overrides,
  };
}

async function confirmDelete(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    await screen.findByRole("button", { name: "Delete contract" }),
  );
  const dialog = await screen.findByRole("alertdialog");
  await user.type(within(dialog).getByRole("textbox"), "Orders");
  await user.click(
    within(dialog).getByRole("button", { name: "Delete Orders" }),
  );
  return dialog;
}

describe("contract layout", () => {
  it("shows Not found for an unknown contract", async () => {
    renderApp("/contracts/c-1", {
      folders: contractFolders,
      handlers: [
        http.get(CONTRACT_API, () => problemResponse(404, "not-found")),
      ],
    });
    expect(
      await screen.findByText(
        "This item does not exist, or you do not have access to it.",
      ),
    ).toBeVisible();
  });

  it("shows any other failure with a retry", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderApp("/contracts/c-1", {
      folders: contractFolders,
      permissions: CONTRACT_ALL,
      handlers: [
        http.get(CONTRACT_API, () => {
          calls += 1;
          return calls === 1
            ? problemResponse(500, "internal")
            : HttpResponse.json(makeContract());
        }),
      ],
    });
    expect(await screen.findByText("Title of internal")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Orders" }),
    ).toBeVisible();
  });

  it("names an unnamed contract by its ODCS id and hides Document with nothing to read", async () => {
    contractPage("/contracts/c-1", {
      contract: makeContract({ name: null, latest_version: null }),
      // A consumer: published reads only, nothing published yet, so no Document tab.
      permissions: ["contract:read-published", "contract:view"],
    });
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "urn:acme:ledger:orders",
      }),
    ).toBeVisible();
    const sections = screen.getByRole("navigation", {
      name: "Contract sections",
    });
    await waitFor(() => {
      expect(
        within(sections)
          .getAllByRole("link")
          .map((link) => link.textContent),
      ).toEqual(["Overview", "Versions", "Diff"]);
    });
  });
});

describe("contract overview facts", () => {
  it("lists a template origin and words every unset value", async () => {
    contractPage("/contracts/c-1", {
      contract: makeContract({
        name: null,
        folder_id: "hidden-id",
        domain: null,
        status: null,
        tags: [],
        api_version: null,
        latest_version: null,
        template: { template_id: "abcdef12-3456", version_no: 4 },
      }),
    });
    const facts = within(
      (await screen.findByText("ODCS apiVersion")).closest("dl") as HTMLElement,
    );
    // A folder the caller cannot see is named by its id.
    expect(facts.getByRole("link", { name: "hidden-id" })).toBeVisible();
    expect(facts.getAllByText("Not set")).toHaveLength(5);
    expect(facts.getByText("Not published")).toBeVisible();
    expect(facts.getByRole("link", { name: "abcdef12" })).toHaveAttribute(
      "href",
      "/templates/abcdef12-3456",
    );
    expect(facts.getByText(/version 4/)).toBeVisible();
  });

  it("links a copied contract to its origin and a published one to its Latest", async () => {
    contractPage("/contracts/c-1", {
      contract: makeContract({ origin: { contract_id: "c-0" } }),
    });
    const facts = within(
      (await screen.findByText("ODCS apiVersion")).closest("dl") as HTMLElement,
    );
    expect(
      facts.getByRole("link", { name: "another contract" }),
    ).toHaveAttribute("href", "/contracts/c-0");
    expect(facts.getByRole("link", { name: "1.0.0" })).toHaveAttribute(
      "href",
      "/contracts/c-1/versions/1.0.0",
    );
    expect(facts.getByText("demo")).toBeVisible();
    expect(facts.queryByText("Created from scratch")).toBeNull();
  });

  it("points a published-only reader at the Versions tab only", async () => {
    contractPage("/contracts/c-1", {
      permissions: ["contract:read-published", "contract:view"],
    });
    expect(
      await screen.findByText("Published versions are on the Versions tab."),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "View versions" })).toBeVisible();
  });
});

describe("contract delete", () => {
  it("moves to the contract list with the deleted notice", async () => {
    const user = userEvent.setup();
    let deleted = 0;
    const { router } = contractPage("/contracts/c-1", {
      handlers: [
        http.delete(CONTRACT_API, () => {
          deleted += 1;
          return new HttpResponse(null, { status: 204 });
        }),
      ],
    });
    await confirmDelete(user);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts");
    });
    expect(router.state.location.search).toEqual({
      deleted: "c-1",
      deletedName: "Orders",
      deletedFolder: "ledger-id",
    });
    expect(deleted).toBe(1);
    expect(await screen.findByText("Orders was deleted")).toBeVisible();
  });

  it("names the cards that pin the contract", async () => {
    const user = userEvent.setup();
    contractPage("/contracts/c-1", {
      handlers: [
        http.delete(CONTRACT_API, () =>
          problemResponse(409, "contract-pinned", {
            reasons: [{ card_name: "Analytics" }, { card_id: "card-7" }],
          }),
        ),
      ],
    });
    await confirmDelete(user);
    expect(
      await screen.findByText("Subscriber cards pin this contract"),
    ).toBeVisible();
    expect(screen.getByText("contract-pinned detail")).toBeVisible();
    expect(screen.getByText("Analytics")).toBeVisible();
    expect(screen.getByText("card-7")).toBeVisible();
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("keeps the dialog open with any other refusal", async () => {
    const user = userEvent.setup();
    contractPage("/contracts/c-1", {
      handlers: [
        http.delete(CONTRACT_API, () => problemResponse(500, "internal")),
      ],
    });
    const dialog = await confirmDelete(user);
    expect(await within(dialog).findByText("Title of internal")).toBeVisible();
    expect(screen.queryByText("This contract is still referenced")).toBeNull();
  });
});

describe("BlockedDelete and reasonsOf", () => {
  it("explains a reference without a referrer list", () => {
    render(
      <BlockedDelete
        problem={httpProblem({
          slug: "contract-referenced",
          extensions: { referrers: "hidden", hidden_referrer_count: "2" },
        })}
      />,
    );
    expect(screen.getByText("This contract is still referenced")).toBeVisible();
    expect(screen.queryByRole("listitem")).toBeNull();
  });

  it("explains a pin without detail or reasons", () => {
    render(
      <BlockedDelete problem={httpProblem({ slug: "contract-pinned" })} />,
    );
    expect(
      screen.getByText("Cards actively pin a version of this contract."),
    ).toBeVisible();
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("shows any other problem as a problem alert", () => {
    render(
      <BlockedDelete
        problem={httpProblem({ slug: "conflict", title: "Something else" })}
      />,
    );
    expect(screen.getByText("Something else")).toBeVisible();
  });

  it("names each reason a card, a string or a placeholder", () => {
    expect(
      reasonsOf(
        httpProblem({
          extensions: {
            reasons: [
              "Pinned by Ops",
              { name: "Billing" },
              { card_id: 7 },
              null,
              5,
            ],
          },
        }),
      ),
    ).toEqual(["Pinned by Ops", "Billing", "A card", "A card", "A card"]);
    expect(reasonsOf(httpProblem({}))).toEqual([]);
    expect(reasonsOf({ kind: "network", status: 0, requestId: null })).toEqual(
      [],
    );
  });
});

describe("contract document tab", () => {
  it("says nothing is published yet and retries a failed document", async () => {
    const user = userEvent.setup();
    let calls = 0;
    contractPage("/contracts/c-1/document", {
      contract: makeContract({ latest_version: null }),
      handlers: [
        http.get(`${CONTRACT_API}/document`, () => {
          calls += 1;
          return calls === 1
            ? problemResponse(500, "internal")
            : textDocument(CONTRACT_DOC);
        }),
      ],
    });
    expect(
      await screen.findByText(
        "The contract's current document (a draft: nothing is published yet).",
      ),
    ).toBeVisible();
    expect(await screen.findByText("Title of internal")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Expand editor" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(
      await screen.findByRole("textbox", { name: "Contract document" }),
    ).toHaveValue(CONTRACT_DOC);
  });

  it("downloads the current document from the page and from full screen", async () => {
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
    const accepts: string[] = [];
    contractPage("/contracts/c-1/document", {
      contract: makeContract({ latest_version: null }),
      handlers: [
        http.get(`${CONTRACT_API}/document`, ({ request }) => {
          accepts.push(request.headers.get("Accept") ?? "");
          return textDocument(CONTRACT_DOC);
        }),
      ],
    });
    await screen.findByRole("textbox", { name: "Contract document" });
    await user.click(screen.getByRole("button", { name: "Download JSON" }));
    await waitFor(() => {
      expect(saved).toEqual(["urn-acme-ledger-orders-current.json"]);
    });
    await user.click(screen.getByRole("button", { name: "Expand editor" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Orders · Document",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Download YAML" }),
    );
    await waitFor(() => {
      expect(saved).toEqual([
        "urn-acme-ledger-orders-current.json",
        "urn-acme-ledger-orders-current.yaml",
      ]);
    });
    expect(accepts).toContain("application/json");
    expect(accepts).toContain("application/yaml");
  });
});
