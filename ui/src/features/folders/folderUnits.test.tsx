// Folder building blocks outside the app shell: the folder queries in api.ts (paging, the 2,000
// cap, empty bodies), FolderSelect / useFolderPathLabel / OwningFolder, and FolderTree's keyboard,
// pointer and current-folder behaviour. A minimal router supplies the `api` root context.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRouteWithContext,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { createApiClient } from "@/api/client";
import type { Folder } from "@/api/types";
import { http, HttpResponse, server } from "@/test/msw";
import { makeFolder, rootFolder, testConfig } from "@/test/renderApp";
import { FOLDER_CAP, useContractCount, useFolder, useFolderList } from "./api";
import { FolderSelect, useRootFolder } from "./FolderSelect";
import { FolderTree } from "./FolderTree";
import { OwningFolder } from "./OwningFolder";

const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const beta = makeFolder({
  id: "beta-id",
  slug: "beta",
  display_name: "Beta",
  path: "beta",
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

function noBody() {
  return new HttpResponse(null, { status: 204 });
}

/** Mounts `ui` at "/" under a router whose root context carries the API client. */
function renderWithRouter(ui: () => ReactNode) {
  const api = createApiClient(testConfig);
  const rootRoute = createRootRouteWithContext<{
    api: typeof api;
  }>()({ component: () => <Outlet /> });
  const index = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <>{ui()}</>,
  });
  const folderRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/folders/$folderId",
    component: () => <p>Folder page</p>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([index, folderRoute]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
    context: { api },
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router };
}

function folderListHandler(folders: Folder[]) {
  return http.get("*/api/v1/identity/folders", () =>
    HttpResponse.json({ items: folders, next_cursor: null }),
  );
}

describe("useFolderList", () => {
  function ListProbe({ archived }: { archived?: boolean }) {
    const list = useFolderList(archived);
    if (list.isError) return <p>error: {list.error.message}</p>;
    if (list.data === undefined) return <p>loading</p>;
    return (
      <p>
        {list.data.folders.length} folders,{" "}
        {list.data.truncated ? "truncated" : "complete"}
      </p>
    );
  }

  it("follows the cursor until the last page", async () => {
    const seen: URLSearchParams[] = [];
    server.use(
      http.get("*/api/v1/identity/folders", ({ request }) => {
        const params = new URL(request.url).searchParams;
        seen.push(params);
        return HttpResponse.json(
          params.get("cursor") === null
            ? { items: [rootFolder, acme], next_cursor: "page-2" }
            : { items: [payments], next_cursor: null },
        );
      }),
    );
    renderWithRouter(() => <ListProbe archived />);
    expect(await screen.findByText("3 folders, complete")).toBeVisible();
    expect(seen.map((params) => params.get("cursor"))).toEqual([
      null,
      "page-2",
    ]);
    expect(seen[0]?.get("limit")).toBe("100");
    expect(seen[0]?.get("include_archived")).toBe("true");
  });

  it("stops at the folder cap and reports the list as truncated", async () => {
    let calls = 0;
    server.use(
      http.get("*/api/v1/identity/folders", ({ request }) => {
        calls += 1;
        expect(new URL(request.url).searchParams.get("include_archived")).toBe(
          "false",
        );
        const items = Array.from({ length: 100 }, (_, index) =>
          makeFolder({ id: `f-${String(calls)}-${String(index)}` }),
        );
        return HttpResponse.json({
          items,
          next_cursor: `after-${String(calls)}`,
        });
      }),
    );
    renderWithRouter(() => <ListProbe />);
    expect(
      await screen.findByText(`${String(FOLDER_CAP)} folders, truncated`),
    ).toBeVisible();
    expect(calls).toBe(FOLDER_CAP / 100);
  });

  it("fails when the folder list has no body", async () => {
    server.use(http.get("*/api/v1/identity/folders", noBody));
    renderWithRouter(() => <ListProbe />);
    expect(
      await screen.findByText("error: GET /folders had no body"),
    ).toBeVisible();
  });
});

describe("useFolder and useContractCount", () => {
  function DetailProbe() {
    const folder = useFolder("acme-id");
    const count = useContractCount("acme-id");
    return (
      <>
        <p>
          folder:{" "}
          {folder.isError
            ? folder.error.message
            : (folder.data?.display_name ?? "loading")}
        </p>
        <p>
          count:{" "}
          {count.isError
            ? count.error.message
            : count.data === undefined
              ? "loading"
              : `${String(count.data.count)}${count.data.more ? "+" : ""}`}
        </p>
      </>
    );
  }

  it("reads one folder and counts one page of its subtree's contracts", async () => {
    let query: URLSearchParams | null = null;
    server.use(
      http.get("*/api/v1/identity/folders/:id", ({ params }) =>
        HttpResponse.json(params.id === "acme-id" ? acme : null),
      ),
      http.get("*/api/v1/contracts/contracts", ({ request }) => {
        query = new URL(request.url).searchParams;
        return HttpResponse.json({
          items: [{ id: "c-1" }, { id: "c-2" }],
          next_cursor: "more",
        });
      }),
    );
    renderWithRouter(() => <DetailProbe />);
    expect(await screen.findByText("folder: Acme")).toBeVisible();
    expect(await screen.findByText("count: 2+")).toBeVisible();
    const sent = query as URLSearchParams | null;
    expect(sent?.get("folder_id")).toBe("acme-id");
    expect(sent?.get("include_subtree")).toBe("true");
    expect(sent?.get("limit")).toBe("100");
  });

  it("fails both reads when the responses have no body", async () => {
    server.use(
      http.get("*/api/v1/identity/folders/:id", noBody),
      http.get("*/api/v1/contracts/contracts", noBody),
    );
    renderWithRouter(() => <DetailProbe />);
    expect(
      await screen.findByText("folder: GET /folders/{id} had no body"),
    ).toBeVisible();
    expect(
      await screen.findByText("count: GET /contracts had no body"),
    ).toBeVisible();
  });
});

describe("FolderSelect, useFolderPathLabel and OwningFolder", () => {
  it("lists folders by path with Root first, filters and reports the choice", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    server.use(folderListHandler([beta, payments, rootFolder, acme]));
    function Picker() {
      const [value, setValue] = useState("");
      const root = useRootFolder();
      return (
        <>
          <p>root: {root?.id ?? "none"}</p>
          <FolderSelect
            id="picker"
            label="Folder"
            placeholder="Choose…"
            helper="Pick one"
            value={value}
            filter={(folder) => folder.id !== "beta-id"}
            onChange={(next) => {
              setValue(next);
              onChange(next);
            }}
          />
        </>
      );
    }
    renderWithRouter(() => <Picker />);
    const select = await screen.findByRole("combobox", { name: "Folder" });
    await waitFor(() => {
      expect(select).toBeEnabled();
    });
    expect(screen.getByText(`root: ${rootFolder.id}`)).toBeVisible();
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([
      "Choose…",
      "Root (all organizations)",
      "Acme (acme)",
      "Payments (acme/payments)",
    ]);
    expect(screen.getByText("Pick one")).toBeVisible();
    await user.selectOptions(select, "payments-id");
    expect(onChange).toHaveBeenCalledWith("payments-id");
    expect(select).toHaveValue("payments-id");
  });

  it("is disabled while the folder list loads and has no root without one", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get("*/api/v1/identity/folders", async () => {
        await gate;
        return HttpResponse.json({ items: [acme], next_cursor: null });
      }),
    );
    function Picker() {
      const root = useRootFolder();
      return (
        <>
          <p>root: {root?.id ?? "none"}</p>
          <FolderSelect
            id="picker"
            label="Folder"
            value=""
            onChange={() => undefined}
          />
        </>
      );
    }
    renderWithRouter(() => <Picker />);
    const select = await screen.findByRole("combobox", { name: "Folder" });
    expect(select).toBeDisabled();
    act(() => {
      release();
    });
    await waitFor(() => {
      expect(select).toBeEnabled();
    });
    expect(screen.getByText("root: none")).toBeVisible();
  });

  it("names the owning folder: Platform, Root, the path or a hidden folder", async () => {
    server.use(folderListHandler([rootFolder, acme, payments]));
    renderWithRouter(() => (
      <ul>
        <li data-testid="platform">
          <OwningFolder folderId={null} />
        </li>
        <li data-testid="undefined">
          <OwningFolder folderId={undefined} />
        </li>
        <li data-testid="root">
          <OwningFolder folderId={rootFolder.id} />
        </li>
        <li data-testid="path">
          <OwningFolder folderId="payments-id" />
        </li>
        <li data-testid="hidden">
          <OwningFolder folderId="secret-id" />
        </li>
      </ul>
    ));
    await waitFor(() => {
      expect(screen.getByTestId("path")).toHaveTextContent("acme/payments");
    });
    expect(screen.getByTestId("platform")).toHaveTextContent("Platform");
    expect(screen.getByTestId("undefined")).toHaveTextContent("Platform");
    expect(screen.getByTestId("root")).toHaveTextContent("Root");
    expect(screen.getByTestId("hidden")).toHaveTextContent(
      "Folder you cannot see",
    );
  });
});

describe("FolderTree", () => {
  const all = [rootFolder, acme, beta, payments, ledger];

  function row(name: string) {
    return screen.getByRole("row", { name });
  }

  it("renders nothing for an empty folder list", async () => {
    renderWithRouter(() => (
      <>
        <p>mounted</p>
        <FolderTree folders={[]} />
      </>
    ));
    expect(await screen.findByText("mounted")).toBeVisible();
    expect(screen.queryByRole("treegrid")).toBeNull();
  });

  it("moves through rows with the arrows, Home, End and type-ahead", async () => {
    const user = userEvent.setup();
    renderWithRouter(() => <FolderTree folders={all} />);
    const tree = await screen.findByRole("treegrid", { name: "Folders" });
    // ROOT starts open with its organizations visible; Acme is collapsed.
    expect(row("Root")).toHaveAttribute("aria-expanded", "true");
    expect(row("Acme")).toHaveAttribute("aria-expanded", "false");
    expect(within(tree).queryByRole("row", { name: "Payments" })).toBeNull();
    expect(row("Root")).toHaveAttribute("tabindex", "0");
    expect(row("Acme")).toHaveAttribute("tabindex", "-1");
    // Header row plus the default actions-free columns.
    expect(
      within(tree)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Name"]);

    row("Root").focus();
    await user.keyboard("{ArrowDown}");
    expect(row("Acme")).toHaveFocus();
    expect(row("Acme")).toHaveAttribute("tabindex", "0");
    await user.keyboard("{ArrowRight}");
    expect(row("Acme")).toHaveAttribute("aria-expanded", "true");
    expect(row("Payments")).toHaveAttribute("aria-level", "3");
    await user.keyboard("{ArrowRight}");
    expect(row("Payments")).toHaveFocus();
    // Payments is collapsed: Left goes to the parent.
    await user.keyboard("{ArrowLeft}");
    expect(row("Acme")).toHaveFocus();
    // Acme is open: Left collapses it.
    await user.keyboard("{ArrowLeft}");
    expect(row("Acme")).toHaveAttribute("aria-expanded", "false");
    await user.keyboard("{End}");
    expect(row("Beta")).toHaveFocus();
    // Down on the last row stays put.
    await user.keyboard("{ArrowDown}");
    expect(row("Beta")).toHaveFocus();
    // Right on a leaf does nothing.
    await user.keyboard("{ArrowRight}");
    expect(row("Beta")).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(row("Acme")).toHaveFocus();
    await user.keyboard("{Home}");
    expect(row("Root")).toHaveFocus();
    // Up on the first row and Left on a top-level collapsed-less root stay put.
    await user.keyboard("{ArrowUp}");
    expect(row("Root")).toHaveFocus();
    // Type-ahead wraps from the focused row: "b" finds Beta, "r" wraps back to Root.
    await user.keyboard("b");
    expect(row("Beta")).toHaveFocus();
    await user.keyboard("r");
    expect(row("Root")).toHaveFocus();
    // No match keeps focus.
    await user.keyboard("z");
    expect(row("Root")).toHaveFocus();
    // Left on the open ROOT collapses it, and Left again (no readable parent) stays put.
    await user.keyboard("{ArrowLeft}");
    expect(row("Root")).toHaveAttribute("aria-expanded", "false");
    expect(within(tree).queryByRole("row", { name: "Acme" })).toBeNull();
    await user.keyboard("{ArrowLeft}");
    expect(row("Root")).toHaveFocus();
  });

  it("leaves unhandled keys alone and ignores keys from row actions", () => {
    renderWithRouter(() => (
      <FolderTree
        folders={all}
        renderActions={(node) => (
          <button type="button">Act on {node.folder.display_name}</button>
        )}
      />
    ));
    return waitFor(() => {
      const root = row("Root");
      // Tab and a bare space-less key such as Shift are not handled (default not prevented).
      expect(fireEvent.keyDown(root, { key: "Tab" })).toBe(true);
      expect(fireEvent.keyDown(root, { key: "Shift" })).toBe(true);
      // An arrow pressed on the row's own action button belongs to the button.
      const action = screen.getByRole("button", { name: "Act on Acme" });
      expect(fireEvent.keyDown(action, { key: "ArrowDown" })).toBe(true);
      expect(root).toHaveAttribute("tabindex", "0");
    });
  });

  it("opens a folder with Enter and Space, and calls onOpen", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const { router } = renderWithRouter(() => (
      <FolderTree folders={all} onOpen={onOpen} />
    ));
    await screen.findByRole("treegrid");
    row("Acme").focus();
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders/acme-id");
    });
    expect(onOpen).toHaveBeenCalledTimes(1);
    await act(async () => {
      await router.navigate({ to: "/" });
    });
    row("Beta").focus();
    await user.keyboard(" ");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders/beta-id");
    });
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("expands and collapses on a row click instead of opening the folder", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const { router } = renderWithRouter(() => (
      <FolderTree folders={all} onOpen={onOpen} />
    ));
    await screen.findByRole("treegrid");
    expect(row("Acme")).toHaveAttribute("aria-expanded", "false");
    await user.click(screen.getByText("Acme"));
    expect(row("Acme")).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getByText("Acme"));
    expect(row("Acme")).toHaveAttribute("aria-expanded", "false");
    // A leaf row click neither opens nor expands anything.
    await user.click(screen.getByText("Beta"));
    expect(row("Beta")).not.toHaveAttribute("aria-expanded");
    expect(router.state.location.pathname).toBe("/");
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("toggles on the chevron and never opens from a row action", async () => {
    const user = userEvent.setup();
    const action = vi.fn();
    const { router } = renderWithRouter(() => (
      <FolderTree
        folders={all}
        label="Navigator"
        columns={[
          {
            id: "kind",
            header: "Kind",
            width: "6rem",
            collapsible: true,
            render: (node) => node.folder.kind,
          },
          {
            id: "path",
            header: "Path",
            width: "8rem",
            align: "end",
            render: (node) => node.folder.path,
          },
        ]}
        actionsColumn={{ header: "Do", width: "3rem" }}
        renderActions={(node, tabbable) => (
          <button type="button" tabIndex={tabbable ? 0 : -1} onClick={action}>
            Act on {node.folder.display_name}
          </button>
        )}
      />
    ));
    const tree = await screen.findByRole("treegrid", { name: "Navigator" });
    expect(
      within(tree)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Name", "Kind", "Path", "Do"]);
    expect(tree.style.getPropertyValue("--tree-cols")).toBe(
      "minmax(0, 1fr) 6rem 8rem 3rem",
    );
    expect(tree.style.getPropertyValue("--tree-cols-narrow")).toBe(
      "minmax(0, 1fr) 8rem 3rem",
    );
    expect(
      within(row("Acme"))
        .getAllByRole("gridcell")
        .map((cell) => cell.textContent),
    ).toEqual(["Acme", "organization", "acme", "Act on Acme"]);
    // Only the tab-stop row's action is tabbable.
    expect(screen.getByRole("button", { name: "Act on ROOT" })).toHaveAttribute(
      "tabindex",
      "0",
    );
    expect(screen.getByRole("button", { name: "Act on Acme" })).toHaveAttribute(
      "tabindex",
      "-1",
    );

    const toggle = row("Acme").querySelector("[data-tree-toggle]");
    expect(toggle).not.toBeNull();
    await user.click(toggle as Element);
    expect(row("Acme")).toHaveAttribute("aria-expanded", "true");
    expect(row("Acme")).toHaveAttribute("tabindex", "0");
    await user.click(toggle as Element);
    expect(row("Acme")).toHaveAttribute("aria-expanded", "false");
    // A leaf has no toggle and no aria-expanded.
    expect(row("Beta").querySelector("[data-tree-toggle]")).toBeNull();
    expect(row("Beta")).not.toHaveAttribute("aria-expanded");

    await user.click(screen.getByRole("button", { name: "Act on Beta" }));
    expect(action).toHaveBeenCalledTimes(1);
    expect(router.state.location.pathname).toBe("/");
  });

  it("marks the current folder, reveals its ancestors and follows a new current folder", async () => {
    const archived = makeFolder({
      id: "old-id",
      display_name: "Old",
      path: "old",
      archived: true,
    });
    const folders = [...all, archived];
    function Switching() {
      const [current, setCurrent] = useState<string | undefined>("ledger-id");
      return (
        <>
          <button
            type="button"
            onClick={() => {
              setCurrent((value) =>
                value === "payments-id" ? undefined : "payments-id",
              );
            }}
          >
            Switch current
          </button>
          <FolderTree folders={folders} currentId={current} />
        </>
      );
    }
    const user = userEvent.setup();
    renderWithRouter(() => <Switching />);
    await screen.findByRole("treegrid");
    // Ledger's ancestors start expanded; Ledger is the current page and the tab stop.
    expect(row("Ledger")).toHaveAttribute("aria-current", "page");
    expect(row("Ledger")).toHaveAttribute("aria-selected", "true");
    expect(row("Ledger")).toHaveAttribute("tabindex", "0");
    expect(row("Acme")).toHaveAttribute("aria-expanded", "true");
    expect(row("Old, archived")).toBeVisible();
    expect(within(row("Old, archived")).getByText("Archived")).toBeVisible();

    // Collapse Acme, then make Payments current: its ancestors open again.
    const acmeToggle = row("Acme").querySelector("[data-tree-toggle]");
    await user.click(acmeToggle as Element);
    expect(screen.queryByRole("row", { name: "Payments" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Switch current" }));
    expect(row("Payments")).toHaveAttribute("aria-current", "page");
    expect(row("Acme")).toHaveAttribute("aria-expanded", "true");
    // Clearing the current folder keeps the tree as it is.
    await user.click(screen.getByRole("button", { name: "Switch current" }));
    expect(row("Payments")).not.toHaveAttribute("aria-current");
    expect(row("Acme")).toHaveAttribute("aria-expanded", "true");
  });
});
