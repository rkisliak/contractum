// /contracts through the real router: the filters (debounced search, submit, ODCS status, domain on
// blur) as typed search params sent to the API, the empty states with and without filters, a list
// failure with retry, keyset "Load more results", client sorting, the cells for unset values, and
// the deleted-contract notice with Restore (success, refusal and without contract:restore).
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import {
  CONTRACT_ALL,
  CONTRACT_READER,
  contractFolders,
  makeContract,
  problemResponse,
} from "@/test/contracts-fixtures";
import { http, HttpResponse } from "@/test/msw";
import { renderApp } from "@/test/renderApp";

const LIST = "*/api/v1/contracts/contracts";

afterEach(() => {
  tokenStore.clear();
});

function listPage(
  path: string,
  permissions: string[],
  answer: Parameters<typeof http.get>[1],
) {
  return renderApp(path, {
    folders: contractFolders,
    permissions,
    handlers: [http.get(LIST, answer)],
  });
}

function rowNames(): string[] {
  const table = screen.getByRole("table", { name: "Contracts" });
  return within(table)
    .getAllByRole("rowheader")
    .map((cell) => cell.textContent);
}

describe("contract list filters", () => {
  it("sends the search, status and domain, and keeps them in the URL", async () => {
    const user = userEvent.setup();
    const queries: URLSearchParams[] = [];
    const { router } = listPage("/contracts", CONTRACT_ALL, ({ request }) => {
      queries.push(new URL(request.url).searchParams);
      return HttpResponse.json({ items: [makeContract()], next_cursor: null });
    });
    await screen.findByRole("link", { name: "Orders" });
    const main = within(screen.getByRole("main"));

    await user.selectOptions(main.getByLabelText("ODCS status"), "draft");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ status: "draft" });
    });

    await user.type(main.getByLabelText("Domain"), " sales ");
    await user.tab();
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        status: "draft",
        domain: "sales",
      });
    });

    // The debounced search applies the trimmed text after 300 ms.
    await user.type(main.getByLabelText("Search contracts"), " ord ");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        q: "ord",
        status: "draft",
        domain: "sales",
      });
    });
    await waitFor(() => {
      const last = queries.at(-1);
      expect(last?.get("q")).toBe("ord");
      expect(last?.get("status")).toBe("draft");
      expect(last?.get("domain")).toBe("sales");
      expect(last?.get("limit")).toBe("50");
    });

    await user.selectOptions(main.getByLabelText("ODCS status"), "");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        q: "ord",
        domain: "sales",
      });
    });
  });

  it("keeps a removed domain removed when the search box changes right after", async () => {
    const user = userEvent.setup();
    const { router } = listPage(
      "/contracts?q=ord&domain=sales",
      CONTRACT_ALL,
      () => HttpResponse.json({ items: [makeContract()], next_cursor: null }),
    );
    await screen.findByRole("link", { name: "Orders" });
    const main = within(screen.getByRole("main"));
    await user.clear(main.getByLabelText("Domain"));
    // Leaving the domain applies it; the search edit lands in the same moment and is debounced.
    await user.clear(main.getByLabelText("Search contracts"));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(router.state.location.search).toEqual({});
  });

  it("applies the search box and the domain at once when the form is submitted", async () => {
    // Browsers do not submit this form on Enter (two text fields and no submit button), but a
    // form submission (requestSubmit, assistive technology) applies both drafts immediately.
    const { router } = listPage("/contracts?status=active", CONTRACT_ALL, () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    );
    const main = within(await screen.findByRole("main"));
    const search = main.getByLabelText("Search contracts");
    fireEvent.change(search, { target: { value: "  orders " } });
    fireEvent.change(main.getByLabelText("Domain"), {
      target: { value: " sales" },
    });
    fireEvent.submit(main.getByRole("search"));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        q: "orders",
        status: "active",
        domain: "sales",
      });
    });
    fireEvent.change(search, { target: { value: " " } });
    fireEvent.change(main.getByLabelText("Domain"), { target: { value: "" } });
    fireEvent.submit(main.getByRole("search"));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ status: "active" });
    });
  });

  it("clears the filters from the no-match state", async () => {
    const user = userEvent.setup();
    const { router } = listPage(
      "/contracts?q=zzz&domain=none",
      CONTRACT_ALL,
      () => HttpResponse.json({ items: [], next_cursor: null }),
    );
    expect(
      await screen.findByText("No contracts match these filters."),
    ).toBeVisible();
    const main = within(screen.getByRole("main"));
    expect(main.getByLabelText("Search contracts")).toHaveValue("zzz");
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    expect(main.getByLabelText("Search contracts")).toHaveValue("");
    expect(main.getByLabelText("Domain")).toHaveValue("");
    expect(await screen.findByText("No contracts yet")).toBeVisible();
    expect(
      screen.getByText("Create the first one with New contract above."),
    ).toBeVisible();
  });

  it("tells a reader where contracts come from and offers no New contract", async () => {
    listPage("/contracts", CONTRACT_READER, () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    );
    expect(
      await screen.findByText(
        "Contracts published in the folders you can see appear here.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "New contract" })).toBeNull();
  });

  it("opens the creation page from New contract", async () => {
    const user = userEvent.setup();
    const { router } = listPage("/contracts", CONTRACT_ALL, () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    );
    await user.click(
      await screen.findByRole("button", { name: "New contract" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/contracts/new");
    });
  });
});

describe("contract list rows", () => {
  it("retries a failed list", async () => {
    const user = userEvent.setup();
    let calls = 0;
    listPage("/contracts", CONTRACT_ALL, () => {
      calls += 1;
      return calls === 1
        ? problemResponse(500, "internal")
        : HttpResponse.json({ items: [makeContract()], next_cursor: null });
    });
    expect(await screen.findByText("Title of internal")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(await screen.findByRole("link", { name: "Orders" })).toBeVisible();
  });

  it("loads more with the cursor, sorts, and words unset cells", async () => {
    const user = userEvent.setup();
    const cursors: (string | null)[] = [];
    listPage("/contracts", CONTRACT_ALL, ({ request }) => {
      const cursor = new URL(request.url).searchParams.get("cursor");
      cursors.push(cursor);
      return cursor === "page-2"
        ? HttpResponse.json({
            items: [
              makeContract({
                id: "c-3",
                name: null,
                odcs_id: "urn:acme:zeta",
                status: null,
                latest_version: null,
                folder_id: "gone-id",
                updated_at: "2026-11-05T10:00:00Z",
              }),
            ],
            next_cursor: null,
          })
        : HttpResponse.json({
            items: [
              makeContract(),
              makeContract({
                id: "c-2",
                name: "Accounts",
                folder_id: "acme-id",
                updated_at: "2026-11-01T09:00:00Z",
              }),
            ],
            next_cursor: "page-2",
          });
    });
    await screen.findByRole("link", { name: "Orders" });
    await user.click(screen.getByRole("button", { name: /Load more/ }));
    const zeta = await screen.findByRole("link", { name: "urn:acme:zeta" });
    expect(cursors).toEqual([null, "page-2"]);
    const zetaRow = zeta.closest("tr");
    expect(zetaRow).not.toBeNull();
    const cells = within(zetaRow as HTMLElement);
    // A folder the caller cannot see, no ODCS status and nothing published.
    expect(cells.getByRole("link", { name: "Folder" })).toBeVisible();
    expect(cells.getByText("Not set")).toBeVisible();
    expect(cells.getByText("Not published")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Name" }));
    expect(rowNames()).toEqual(["Accounts", "Orders", "urn:acme:zeta"]);
    await user.click(screen.getByRole("button", { name: "Folder" }));
    expect(rowNames()).toEqual(["Accounts", "Orders", "urn:acme:zeta"]);
    await user.click(screen.getByRole("button", { name: "Updated" }));
    expect(rowNames()).toEqual(["Accounts", "Orders", "urn:acme:zeta"]);
    await user.click(screen.getByRole("button", { name: "Updated" }));
    expect(rowNames()).toEqual(["urn:acme:zeta", "Orders", "Accounts"]);
  });
});

describe("deleted contract notice", () => {
  const DELETED =
    "/contracts?deleted=c-1&deletedName=Orders&deletedFolder=ledger-id";

  it("restores the contract and links to it", async () => {
    const user = userEvent.setup();
    let restored = 0;
    const { router } = renderApp(DELETED, {
      folders: contractFolders,
      permissions: CONTRACT_ALL,
      handlers: [
        http.post(`${LIST}/c-1/restore`, () => {
          restored += 1;
          return HttpResponse.json(makeContract());
        }),
      ],
    });
    expect(await screen.findByText("Orders was deleted")).toBeVisible();
    expect(
      screen.getByText(
        "It no longer appears in lists or fetches. An Organization admin can restore it.",
      ),
    ).toBeVisible();
    await user.click(
      await screen.findByRole("button", { name: "Restore contract" }),
    );
    expect(await screen.findByText("Orders was restored")).toBeVisible();
    expect(restored).toBe(1);
    expect(screen.getByRole("link", { name: "Open Orders" })).toHaveAttribute(
      "href",
      "/contracts/c-1",
    );
    await user.click(screen.getByRole("button", { name: "Dismiss alert" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    expect(screen.queryByText("Orders was restored")).toBeNull();
  });

  it("keeps the notice with the refusal when the restore fails", async () => {
    const user = userEvent.setup();
    renderApp(DELETED, {
      folders: contractFolders,
      permissions: CONTRACT_ALL,
      handlers: [
        http.post(`${LIST}/c-1/restore`, () =>
          problemResponse(409, "contract-odcs-id-taken"),
        ),
      ],
    });
    await user.click(
      await screen.findByRole("button", { name: "Restore contract" }),
    );
    expect(
      await screen.findByText("Title of contract-odcs-id-taken"),
    ).toBeVisible();
    expect(screen.getByText("Orders was deleted")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Restore contract" }),
    ).toBeEnabled();
  });

  it("offers no restore without contract:restore and names an unnamed contract generically", async () => {
    renderApp("/contracts?deleted=c-1", {
      folders: contractFolders,
      permissions: CONTRACT_READER,
    });
    expect(await screen.findByText("The contract was deleted")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Restore contract" }),
    ).toBeNull();
  });
});
