// The Diff tab through the real router: the ref pickers (Latest, versions with their status, drafts
// for a contract:read holder only), Swap and clearing a side, the grouped change list with the
// required bump, counts, the truncation notice and "Load more changes", the side-by-side JSON
// documents (a draft side read from the draft document), and the failure states.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import {
  CONTRACT_ALL,
  CONTRACT_API,
  contractFolders,
  contractHandlers,
  makeDraft,
  makeVersion,
  problemResponse,
  textDocument,
} from "@/test/contracts-fixtures";
import { http, HttpResponse } from "@/test/msw";
import { renderApp } from "@/test/renderApp";

const DRAFT_ID = "0f0e0d0c-0b0a-4908-8706-050403020100";

const versions = [
  makeVersion("1.2.0", { status: "preview" }),
  makeVersion("1.1.0", { status: "latest" }),
  makeVersion("1.0.0", { lifecycle: "unpublished" }),
];
const drafts = [
  makeDraft(DRAFT_ID, { revision: 3, declared_version: "2.0.0" }),
  makeDraft("d-2", { declared_version: null }),
];

function change(
  code: string,
  klass: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    code,
    class: klass,
    path: "schema.orders",
    attribute: null,
    before: null,
    after: null,
    pointer_before: null,
    pointer_after: null,
    ...overrides,
  };
}

function diffPage(overrides: Record<string, unknown> = {}) {
  return {
    changes: [],
    changes_total: 0,
    classifier_version: "1",
    counts: {},
    from: {
      ref: "1.0.0",
      kind: "version",
      content_sha256: "a",
      semver: "1.0.0",
    },
    to: { ref: "1.1.0", kind: "version", content_sha256: "b", semver: "1.1.0" },
    next_cursor: null,
    required_bump: "none",
    truncated: false,
    ...overrides,
  };
}

function diffTab(path: string, extra: Parameters<typeof renderApp>[1] = {}) {
  return renderApp(path, {
    folders: contractFolders,
    permissions: CONTRACT_ALL,
    ...extra,
    handlers: [
      ...(extra.handlers ?? []),
      ...contractHandlers({ versions, drafts }),
    ],
  });
}

afterEach(() => {
  tokenStore.clear();
});

describe("diff tab pickers", () => {
  it("offers Latest, every version with its status and the drafts, and asks for two refs", async () => {
    diffTab("/contracts/c-1/diff");
    expect(
      await screen.findByText("Pick two versions to compare"),
    ).toBeVisible();
    const from = screen.getByLabelText("From (older)");
    await waitFor(() => {
      expect(
        within(from)
          .getAllByRole("option")
          .map((option) => option.textContent),
      ).toEqual([
        "Choose a version",
        "Latest",
        "1.2.0 (Preview)",
        "1.1.0 (Latest)",
        "1.0.0 (Unpublished)",
        "Draft 2.0.0 (revision 3)",
        "Draft  (revision 1)",
      ]);
    });
  });

  it("puts the picks in the URL, swaps them and clears a side", async () => {
    const user = userEvent.setup();
    const { router } = diffTab("/contracts/c-1/diff", {
      handlers: [
        http.get(`${CONTRACT_API}/diff`, () => HttpResponse.json(diffPage())),
        http.get(`${CONTRACT_API}/versions/:ref`, () =>
          textDocument("{}", "application/json"),
        ),
      ],
    });
    const from = await screen.findByLabelText("From (older)");
    await within(from).findByRole("option", { name: "1.0.0 (Unpublished)" });
    await user.selectOptions(from, "1.0.0");
    await user.selectOptions(screen.getByLabelText("To (newer)"), "latest");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        from: "1.0.0",
        to: "latest",
      });
    });
    await user.click(screen.getByRole("button", { name: "Swap" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        from: "latest",
        to: "1.0.0",
      });
    });
    await user.selectOptions(screen.getByLabelText("To (newer)"), "");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ from: "latest" });
    });
    await user.selectOptions(screen.getByLabelText("From (older)"), "");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    expect(screen.getByText("Pick two versions to compare")).toBeVisible();
  });

  it("never submits the picker form", async () => {
    diffTab("/contracts/c-1/diff");
    const form = (await screen.findByLabelText("From (older)")).closest("form");
    expect(form).not.toBeNull();
    // fireEvent returns false when the handler prevented the default (a page reload).
    expect(fireEvent.submit(form as HTMLFormElement)).toBe(false);
  });

  it("asks again when both sides name the same ref", async () => {
    diffTab("/contracts/c-1/diff?from=1.1.0&to=1.1.0");
    expect(
      await screen.findByText("Pick two versions to compare"),
    ).toBeVisible();
  });

  it("offers no drafts and requests none without contract:read", async () => {
    let draftsRequested = false;
    renderApp("/contracts/c-1/diff", {
      folders: contractFolders,
      permissions: ["contract:read-published", "contract:view"],
      handlers: [
        http.get(`${CONTRACT_API}/drafts`, () => {
          draftsRequested = true;
          return HttpResponse.json({ items: drafts });
        }),
        ...contractHandlers({ versions }),
      ],
    });
    const from = await screen.findByLabelText("From (older)");
    await within(from).findByRole("option", { name: "1.2.0 (Preview)" });
    expect(screen.queryAllByRole("option", { name: /Draft/ })).toEqual([]);
    expect(draftsRequested).toBe(false);
  });
});

describe("diff tab results", () => {
  it("groups the changes, pages with the cursor and shows both documents side by side", async () => {
    const user = userEvent.setup();
    const diffQueries: URLSearchParams[] = [];
    const documentAccepts: string[] = [];
    diffTab(`/contracts/c-1/diff?from=1.0.0&to=${DRAFT_ID}`, {
      handlers: [
        http.get(`${CONTRACT_API}/diff`, ({ request }) => {
          const query = new URL(request.url).searchParams;
          diffQueries.push(query);
          return query.get("cursor") === "next-1"
            ? HttpResponse.json(
                diffPage({
                  changes: [
                    change("description-changed", "info", {
                      path: "",
                      before: "old",
                      after: "new",
                    }),
                  ],
                }),
              )
            : HttpResponse.json(
                diffPage({
                  required_bump: "major",
                  counts: { breaking: 2, info: 1 },
                  changes_total: 3,
                  truncated: true,
                  next_cursor: "next-1",
                  changes: [
                    change("property-removed", "breaking", {
                      attribute: "type",
                      before: { type: "string" },
                      after: null,
                    }),
                    change("required-added", "breaking"),
                  ],
                }),
              );
        }),
        http.get(`${CONTRACT_API}/versions/1.0.0`, ({ request }) => {
          documentAccepts.push(request.headers.get("Accept") ?? "");
          return textDocument(
            '{"id":"urn:a","version":"1.0.0"}',
            "application/json",
          );
        }),
        http.get(
          `${CONTRACT_API}/drafts/${DRAFT_ID}/document`,
          ({ request }) => {
            documentAccepts.push(request.headers.get("Accept") ?? "");
            // Not JSON: shown as returned.
            return textDocument("id: urn:a\n", "application/json");
          },
        ),
      ],
    });
    expect(await screen.findByText("Required bump: major")).toBeVisible();
    expect(screen.getByText("Breaking: 2")).toBeVisible();
    expect(screen.getByText("Warning: 0")).toBeVisible();
    expect(screen.getByText("Info: 1")).toBeVisible();
    expect(screen.getByText("3 changes in total")).toBeVisible();
    expect(screen.getByText("Showing the first 10,000 changes.")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Breaking (2)" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: /^Warning/ })).toBeNull();
    expect(screen.getByText("Property removed")).toBeVisible();
    expect(screen.getByText("schema.orders · type")).toBeVisible();
    expect(screen.getByText('{"type":"string"} → not set')).toBeVisible();
    // A change with neither side set shows no value line.
    const required = screen.getByText("Required added").closest("li");
    expect(required).not.toBeNull();
    expect(required).not.toHaveTextContent("→");
    expect(diffQueries[0]?.get("limit")).toBe("500");
    expect(diffQueries[0]?.has("cursor")).toBe(false);

    await user.click(screen.getByRole("button", { name: "Load more changes" }));
    expect(await screen.findByText("Description changed")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Info (1)" })).toBeVisible();
    expect(screen.getByText("(contract)")).toBeVisible();
    expect(screen.getByText("old → new")).toBeVisible();
    expect(diffQueries.at(-1)?.get("cursor")).toBe("next-1");
    expect(
      screen.queryByRole("button", { name: "Load more changes" }),
    ).toBeNull();

    expect(await screen.findByLabelText("Before")).toHaveTextContent(
      '{ "id": "urn:a", "version": "1.0.0" }',
    );
    expect(screen.getByLabelText("After")).toHaveTextContent("id: urn:a");
    expect(documentAccepts).toEqual(["application/json", "application/json"]);
  });

  it("says when the documents are equivalent and shows a document failure", async () => {
    diffTab("/contracts/c-1/diff?from=1.0.0&to=1.1.0", {
      handlers: [
        http.get(`${CONTRACT_API}/diff`, () =>
          HttpResponse.json(diffPage({ required_bump: "minor" })),
        ),
        http.get(`${CONTRACT_API}/versions/:ref`, () =>
          problemResponse(500, "documents-unavailable"),
        ),
      ],
    });
    expect(
      await screen.findByText("The two documents are equivalent: no changes."),
    ).toBeVisible();
    expect(screen.getByText("Required bump: minor")).toBeVisible();
    expect(screen.queryByText(/Showing the first/)).toBeNull();
    expect(
      await screen.findByText("Title of documents-unavailable"),
    ).toBeVisible();
    expect(screen.queryByLabelText("Before")).toBeNull();
  });

  it("shows a neutral bump and a diff failure with a retry", async () => {
    const user = userEvent.setup();
    let calls = 0;
    diffTab("/contracts/c-1/diff?from=1.0.0&to=1.1.0", {
      handlers: [
        http.get(`${CONTRACT_API}/diff`, () => {
          calls += 1;
          return calls === 1
            ? problemResponse(422, "diff-refused")
            : HttpResponse.json(diffPage({ required_bump: "patch" }));
        }),
        http.get(`${CONTRACT_API}/versions/:ref`, () =>
          textDocument("{}", "application/json"),
        ),
      ],
    });
    expect(await screen.findByText("Title of diff-refused")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(await screen.findByText("Required bump: patch")).toBeVisible();
    expect(calls).toBe(2);
  });
});
