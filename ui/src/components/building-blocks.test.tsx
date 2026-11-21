// Shared building blocks not covered by controls.test.tsx and feedback.test.tsx: DataTable
// (sorting, load more, page errors), Tabs and TabNav, NotFound, Breadcrumb, PageHeader,
// Menu links, ButtonLink, Breakable, Stat, TextareaField, SelectField, NotAvailable, Spinner
// without matchMedia, Card (plain and band header), Dialog without dismissal or with a labelled
// close, Drawer variants, Identifier, WhenBy, Logo, IconButtonLink and a Menu with a trigger tooltip.
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Activity, ExternalLink, Minimize2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/problem";
import { Breadcrumb } from "./Breadcrumb";
import { Breakable } from "./Breakable";
import { ButtonLink, IconButtonLink } from "./Button";
import { Card } from "./Card";
import cardStyles from "./Card/Card.module.css";
import { DataTable, orNotSet, type Column } from "./DataTable";
import { WhenBy } from "./DataTable/WhenBy";
import { Dialog } from "./Dialog";
import { Drawer } from "./Drawer";
import { SelectField, TextareaField } from "./form";
import { Identifier } from "./Identifier";
import { Logo } from "./Logo";
import logoStyles from "./Logo/Logo.module.css";
import { Menu } from "./Menu";
import { NotFound } from "./NotFound";
import { frameCrumbs, PageHeader } from "./PageHeader";
import { Stat, StatGrid } from "./Stat";
import { NotAvailable, Spinner } from "./states";
import { Tabs, TabNav } from "./Tabs";
import { TooltipProvider } from "./Tooltip";

/** Mounts `ui` at `path` under a router with a few named destinations. */
function renderRouted(ui: () => ReactNode, path = "/") {
  const rootRoute = createRootRoute({ component: () => <Outlet /> });
  const page = (url: string) =>
    createRoute({
      getParentRoute: () => rootRoute,
      path: url,
      component: () => <>{ui()}</>,
    });
  const router = createRouter({
    routeTree: rootRoute.addChildren([
      page("/"),
      page("/folders"),
      page("/folders/$folderId"),
      page("/folders/$folderId/contracts"),
    ]),
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(<RouterProvider router={router} />);
  return { router };
}

type Row = { id: string; name: string; size: number; note: string | null };
const rows: Row[] = [
  { id: "b", name: "Beta", size: 10, note: "x" },
  { id: "a", name: "alpha", size: 2, note: null },
  { id: "c", name: "Gamma", size: 33, note: "" },
];
const columns: Column<Row>[] = [
  {
    id: "name",
    header: "Name",
    rowHeader: true,
    sortValue: (row) => row.name,
    cell: (row) => row.name,
  },
  {
    id: "size",
    header: "Size",
    align: "end",
    sortValue: (row) => row.size,
    cell: (row) => row.size,
  },
  { id: "note", header: "Note", cell: (row) => orNotSet(row.note) },
];

function bodyNames() {
  const table = screen.getByRole("table");
  return within(table)
    .getAllByRole("rowheader")
    .map((cell) => cell.textContent);
}

describe("DataTable", () => {
  it("sorts by text and by number, ascending, descending, then back to the given order", async () => {
    const user = userEvent.setup();
    render(
      <DataTable
        caption="Things"
        captionHidden
        columns={columns}
        rows={rows}
        getRowId={(row) => row.id}
      />,
    );
    expect(screen.getByRole("region", { name: "Things" })).toHaveAttribute(
      "tabindex",
      "0",
    );
    expect(bodyNames()).toEqual(["Beta", "alpha", "Gamma"]);
    const nameHeader = screen.getByRole("columnheader", { name: "Name" });
    const sizeHeader = screen.getByRole("columnheader", { name: "Size" });
    expect(nameHeader).toHaveAttribute("aria-sort", "none");
    expect(
      screen.getByRole("columnheader", { name: "Note" }),
    ).not.toHaveAttribute("aria-sort");

    await user.click(within(nameHeader).getByRole("button"));
    expect(nameHeader).toHaveAttribute("aria-sort", "ascending");
    expect(bodyNames()).toEqual(["alpha", "Beta", "Gamma"]);
    await user.click(within(nameHeader).getByRole("button"));
    expect(nameHeader).toHaveAttribute("aria-sort", "descending");
    expect(bodyNames()).toEqual(["Gamma", "Beta", "alpha"]);
    await user.click(within(nameHeader).getByRole("button"));
    expect(nameHeader).toHaveAttribute("aria-sort", "none");
    expect(bodyNames()).toEqual(["Beta", "alpha", "Gamma"]);

    // Numbers compare numerically (2 < 10 < 33), not as text.
    await user.click(within(sizeHeader).getByRole("button"));
    expect(sizeHeader).toHaveAttribute("aria-sort", "ascending");
    expect(bodyNames()).toEqual(["alpha", "Beta", "Gamma"]);
    await user.click(within(sizeHeader).getByRole("button"));
    expect(bodyNames()).toEqual(["Gamma", "Beta", "alpha"]);
    expect(nameHeader).toHaveAttribute("aria-sort", "none");
  });

  it("renders Not set for null and empty values, and keeps a value", () => {
    render(
      <DataTable
        caption="Things"
        columns={columns}
        rows={rows}
        getRowId={(row) => row.id}
      />,
    );
    expect(screen.getAllByText("Not set")).toHaveLength(2);
    expect(screen.getByText("x")).toBeVisible();
    expect(screen.queryByText("All results loaded")).toBeNull();
  });

  it("loads more, focuses the first new row and announces the count", async () => {
    const user = userEvent.setup();
    function Paged() {
      const [list, setList] = useState(rows.slice(0, 1));
      const hasMore = list.length < rows.length;
      return (
        <DataTable
          caption="Paged"
          columns={columns}
          rows={list}
          getRowId={(row) => row.id}
          loadMore={{
            hasMore,
            isLoadingMore: false,
            onLoadMore: () => {
              setList(rows);
            },
          }}
        />
      );
    }
    render(<Paged />);
    expect(screen.getByText("Showing 1 loaded")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Load more results" }));
    expect(await screen.findByText("2 more loaded")).toBeInTheDocument();
    const second = screen
      .getByRole("rowheader", { name: "alpha" })
      .closest("tr");
    expect(second).toHaveFocus();
    expect(screen.getByText("All results loaded")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Load more results" }),
    ).toBeNull();
  });

  it("does not move focus when rows change without a Load more", () => {
    const { rerender } = render(
      <DataTable
        caption="Live"
        columns={columns}
        rows={rows.slice(0, 1)}
        getRowId={(row) => row.id}
      />,
    );
    rerender(
      <DataTable
        caption="Live"
        columns={columns}
        rows={rows}
        getRowId={(row) => row.id}
      />,
    );
    expect(document.body).toHaveFocus();
  });

  it("shows a busy Load more and a retryable alert for a failed page", async () => {
    const user = userEvent.setup();
    const onLoadMore = vi.fn();
    render(
      <DataTable
        caption="Failing"
        columns={columns.slice(0, 2)}
        compact
        rows={rows}
        getRowId={(row) => row.id}
        loadMore={{
          hasMore: true,
          isLoadingMore: true,
          onLoadMore,
          error: new ApiError({
            kind: "problem",
            status: 500,
            slug: "internal",
            title: "Page two failed",
            detail: null,
            errors: [],
            requestId: null,
            extensions: {},
          }),
        }}
      />,
    );
    expect(screen.getByText("Page two failed")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
    const more = screen.getByRole("button", { name: /Load/ });
    expect(more).toHaveAttribute("aria-busy", "true");
  });
});

describe("Tabs and TabNav", () => {
  it("switches panels in place, controlled or not", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(
      <Tabs
        label="Sections"
        onValueChange={onValueChange}
        items={[
          { value: "one", label: "One", content: <p>First panel</p> },
          { value: "two", label: "Two", content: <p>Second panel</p> },
        ]}
      />,
    );
    const list = screen.getByRole("tablist", { name: "Sections" });
    expect(within(list).getByRole("tab", { name: "One" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("First panel")).toBeVisible();
    await user.click(within(list).getByRole("tab", { name: "Two" }));
    expect(onValueChange).toHaveBeenCalledWith("two");
    expect(screen.getByText("Second panel")).toBeVisible();
    expect(screen.queryByText("First panel")).toBeNull();
  });

  it("starts on the default tab", () => {
    render(
      <Tabs
        label="Sections"
        defaultValue="two"
        items={[
          { value: "one", label: "One", content: <p>First panel</p> },
          { value: "two", label: "Two", content: <p>Second panel</p> },
        ]}
      />,
    );
    expect(screen.getByRole("tab", { name: "Two" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("marks the current route link and hides a single-tab nav", async () => {
    renderRouted(
      () => (
        <>
          <TabNav
            label="Folder sections"
            items={[
              { to: "/folders/f1", label: "Overview", exact: true },
              { to: "/folders/f1/contracts", label: "Contracts" },
            ]}
          />
          <TabNav label="Lonely" items={[{ to: "/", label: "Only" }]} />
        </>
      ),
      "/folders/f1/contracts",
    );
    const nav = await screen.findByRole("navigation", {
      name: "Folder sections",
    });
    expect(
      within(nav).getByRole("link", { name: "Contracts" }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      within(nav).getByRole("link", { name: "Overview" }),
    ).not.toHaveAttribute("aria-current");
    expect(screen.queryByRole("navigation", { name: "Lonely" })).toBeNull();
  });
});

describe("NotFound", () => {
  it("names the missing item, focuses the heading and offers home and back", async () => {
    const user = userEvent.setup();
    const { router } = renderRouted(
      () => <NotFound back={{ label: "folders", to: "/folders" }} />,
      "/folders/missing",
    );
    const heading = await screen.findByRole("heading", {
      level: 1,
      name: "Not found",
    });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
    expect(
      screen.getByText(
        "This item does not exist, or you do not have access to it.",
      ),
    ).toBeVisible();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(crumbs).getByRole("link", { name: "Folders" }),
    ).toHaveAttribute("href", "/folders");
    await user.click(screen.getByRole("button", { name: "Back to folders" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders");
    });
    await user.click(await screen.findByRole("button", { name: "Go to home" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/");
    });
  });

  it("renders a nested h2 for an unknown URL without a back action", async () => {
    renderRouted(() => (
      <>
        <h1>Page</h1>
        <NotFound nested unknownUrl />
      </>
    ));
    expect(
      await screen.findByRole("heading", { level: 2, name: "Not found" }),
    ).toBeVisible();
    expect(screen.getByText("This page does not exist.")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Back to/ })).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
  });
});

describe("Breadcrumb and PageHeader", () => {
  it("gives every crumb its full label as the title, so a long name can end in an ellipsis (V-02)", async () => {
    const long = "VolumeLongNamedTeam".repeat(12);
    renderRouted(() => (
      <Breadcrumb
        items={[
          { label: long, to: "/folders/a" },
          { label: long.toLowerCase() },
        ]}
      />
    ));
    const nav = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(within(nav).getByRole("link", { name: long })).toHaveAttribute(
      "title",
      long,
    );
    expect(within(nav).getByText(long.toLowerCase())).toHaveAttribute(
      "title",
      long.toLowerCase(),
    );
  });

  it("renders a crumb without a target as plain text and nothing for no items", async () => {
    renderRouted(() => (
      <>
        <Breadcrumb
          items={[
            { label: "Home", to: "/" },
            { label: "Governance" },
            { label: "Templates" },
          ]}
        />
        <Breadcrumb items={[]} />
      </>
    ));
    const nav = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(within(nav).getByRole("link", { name: "Home" })).toBeVisible();
    expect(within(nav).getByText("Governance").tagName).toBe("SPAN");
    expect(within(nav).getByText("Governance")).not.toHaveAttribute(
      "aria-current",
    );
    expect(within(nav).getByText("Templates")).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getAllByRole("navigation")).toHaveLength(1);
  });

  it("frames the trail with Home and the current page", () => {
    expect(frameCrumbs([], "Home")).toEqual([{ label: "Home" }]);
    expect(frameCrumbs([], "Folders")).toEqual([
      { label: "Home", to: "/" },
      { label: "Folders" },
    ]);
    expect(
      frameCrumbs(
        [
          { label: "Home", to: "/" },
          { label: "Folders", to: "/folders" },
          { label: "Acme" },
        ],
        "Acme",
      ),
    ).toEqual([
      { label: "Home", to: "/" },
      { label: "Folders", to: "/folders" },
      { label: "Acme" },
    ]);
  });

  it("uses documentTitle for the tab and crumb, with badges, actions and description", async () => {
    renderRouted(() => (
      <PageHeader
        title="Welcome, Ada"
        documentTitle="Home"
        badges={<span>Badge</span>}
        actions={<button type="button">Act</button>}
        description="About this page"
      />
    ));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Welcome, Ada" }),
    ).toBeVisible();
    await waitFor(() => {
      expect(document.title).toBe("Home · Contractum");
    });
    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(nav).getByText("Home")).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByText("Badge")).toBeVisible();
    expect(screen.getByRole("button", { name: "Act" })).toBeVisible();
    expect(screen.getByText("About this page")).toBeVisible();
  });
});

describe("Menu and ButtonLink", () => {
  it("renders link items, a header and a divider before danger items", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn();
    const { router } = renderRouted(() => (
      <Menu
        trigger={<button type="button">Open menu</button>}
        header={<p>Signed in as Ada</p>}
        items={[
          { label: "Folders", to: "/folders", icon: ExternalLink },
          { label: "Disabled", onSelect: vi.fn(), disabled: true },
          { label: "Delete", danger: true, onSelect: onDelete },
        ]}
      />
    ));
    await user.click(await screen.findByRole("button", { name: "Open menu" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByText("Signed in as Ada")).toBeVisible();
    expect(within(menu).getAllByRole("separator")).toHaveLength(2);
    expect(
      within(menu).getByRole("menuitem", { name: "Disabled" }),
    ).toHaveAttribute("data-disabled");
    await user.click(within(menu).getByRole("menuitem", { name: "Folders" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders");
    });
    expect(onDelete).not.toHaveBeenCalled();
  });

  it("renders only danger items without a divider", async () => {
    const user = userEvent.setup();
    renderRouted(() => (
      <Menu
        trigger={<button type="button">Open menu</button>}
        items={[{ label: "Delete", danger: true }]}
      />
    ));
    await user.click(await screen.findByRole("button", { name: "Open menu" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).queryByRole("separator")).toBeNull();
    expect(
      within(menu).getByRole("menuitem", { name: "Delete" }),
    ).toBeVisible();
  });

  it("ButtonLink is a router link drawn as a button", async () => {
    const user = userEvent.setup();
    const { router } = renderRouted(() => (
      <ButtonLink
        to="/folders"
        variant="secondary"
        size="small"
        icon={Activity}
      >
        All folders
      </ButtonLink>
    ));
    const link = await screen.findByRole("link", { name: "All folders" });
    expect(link).toHaveAttribute("href", "/folders");
    expect(link).toHaveAttribute("data-button", "small");
    await user.click(link);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders");
    });
  });
});

describe("small display components", () => {
  it("Breakable keeps the whole value as text with break points after separators", () => {
    const { container } = render(
      <p data-testid="value">
        <Breakable text="acme/payments.ledger:v1" />
      </p>,
    );
    expect(screen.getByTestId("value")).toHaveTextContent(
      "acme/payments.ledger:v1",
    );
    expect(container.querySelectorAll("wbr")).toHaveLength(3);
  });

  it("Stat pairs each label with its value and optional hint", () => {
    render(
      <StatGrid label="Account summary">
        <Stat label="Folders" value={12} icon={Activity} hint="Across orgs" />
        <Stat label="Grants" value={3} icon={Activity} />
      </StatGrid>,
    );
    const grid = screen.getByLabelText("Account summary");
    expect(grid.tagName).toBe("DL");
    const terms = within(grid).getAllByRole("term");
    expect(terms.map((term) => term.textContent)).toEqual([
      "Folders",
      "Grants",
    ]);
    const values = within(grid).getAllByRole("definition");
    expect(values.map((value) => value.textContent)).toEqual([
      "12",
      "Across orgs",
      "3",
    ]);
  });

  it("NotAvailable is a calm empty state naming what is not included", () => {
    render(<NotAvailable what="The drift report" level={3} />);
    expect(
      screen.getByRole("heading", {
        level: 3,
        name: "Not available at your access level",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "The drift report is not included in your access. Ask a folder admin if you need it.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("Spinner animates when matchMedia is unavailable", () => {
    const original = Object.getOwnPropertyDescriptor(window, "matchMedia");
    Reflect.deleteProperty(window, "matchMedia");
    try {
      expect("matchMedia" in window).toBe(false);
      const { unmount } = render(<Spinner label="Working" />);
      expect(screen.getByRole("status")).toHaveTextContent("Working");
      expect(document.querySelector("svg")).not.toBeNull();
      unmount();
    } finally {
      if (original) Object.defineProperty(window, "matchMedia", original);
    }
  });

  it("Card renders a page-size title, a footer, and an unlabelled section without a title", () => {
    render(
      <>
        <Card
          title="Sign in"
          titleAs="h1"
          titleSize="page"
          footer={<p>Foot</p>}
        >
          <p>Body</p>
        </Card>
        <Card>
          <p>Untitled body</p>
        </Card>
      </>,
    );
    expect(screen.getByRole("region", { name: "Sign in" })).toContainElement(
      screen.getByText("Foot"),
    );
    expect(
      screen.getByRole("heading", { level: 1, name: "Sign in" }),
    ).toBeVisible();
    const untitled = screen.getByText("Untitled body").closest("section");
    expect(untitled).not.toHaveAttribute("aria-labelledby");
  });
});

describe("form fields", () => {
  it("TextareaField is labelled, monospace on request, and reports errors", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <>
        <TextareaField
          id="doc"
          label="Document"
          helper="YAML or JSON"
          error="Does not parse"
          monospace
          onChange={(event) => {
            onChange(event.target.value);
          }}
        />
        <TextareaField id="ro" label="Read only" readOnly value="fixed" />
        <TextareaField id="off" label="Disabled" disabled rows={2} />
      </>,
    );
    const doc = screen.getByRole("textbox", { name: "Document" });
    expect(doc).toHaveAttribute("rows", "4");
    expect(doc).toHaveAttribute("aria-invalid", "true");
    expect(doc).toHaveAccessibleDescription(/Does not parse/);
    await user.type(doc, "a: 1");
    expect(onChange).toHaveBeenLastCalledWith("a: 1");
    expect(screen.getByRole("textbox", { name: "Read only" })).toHaveAttribute(
      "readonly",
    );
    const disabled = screen.getByRole("textbox", { name: "Disabled" });
    expect(disabled).toBeDisabled();
    expect(disabled).toHaveAttribute("rows", "2");
    expect(disabled.parentElement).toHaveAttribute("data-control", "multiline");
  });

  it("SelectField offers a placeholder and disabled options", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <SelectField
        id="domain"
        label="Domain"
        placeholder="Any domain"
        value=""
        onChange={(event) => {
          onChange(event.target.value);
        }}
        options={[
          { value: "sales", label: "Sales" },
          { value: "hr", label: "HR", disabled: true },
        ]}
      />,
    );
    const select = screen.getByRole("combobox", { name: "Domain" });
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Any domain", "Sales", "HR"]);
    expect(within(select).getByRole("option", { name: "HR" })).toBeDisabled();
    await user.selectOptions(select, "sales");
    expect(onChange).toHaveBeenCalledWith("sales");
  });
});

describe("Dialog and Drawer variants", () => {
  it("a non-dismissible dialog has no close button and ignores Escape", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(
      <Dialog
        open
        onOpenChange={onOpenChange}
        title="Store it"
        dismissible={false}
        size={896}
      >
        <p>Secret body</p>
      </Dialog>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Store it" });
    expect(
      within(dialog).queryByRole("button", { name: "Close dialog" }),
    ).toBeNull();
    await user.keyboard("{Escape}");
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(dialog).toBeVisible();
  });

  it("a dismissible dialog closes from its close button and refocuses the opener", async () => {
    const user = userEvent.setup();
    function Opener() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button
            type="button"
            onClick={() => {
              setOpen(true);
            }}
          >
            Open it
          </button>
          <Dialog
            open={open}
            onOpenChange={setOpen}
            title="Plain"
            description="Words"
          >
            <p>Body</p>
          </Dialog>
        </>
      );
    }
    render(<Opener />);
    const opener = screen.getByRole("button", { name: "Open it" });
    await user.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Plain" });
    expect(dialog).toHaveAccessibleDescription("Words");
    await user.click(
      within(dialog).getByRole("button", { name: "Close dialog" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(opener).toHaveFocus();
  });

  it("an end drawer is padded, can be wide and can hide its title visually", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(
      <Drawer
        open
        onOpenChange={onOpenChange}
        title="Event details"
        side="end"
        width="wide"
        hideTitle
      >
        <p>Drawer body</p>
      </Drawer>,
    );
    const drawer = await screen.findByRole("dialog", { name: "Event details" });
    expect(within(drawer).getByText("Drawer body")).toBeVisible();
    await user.click(
      within(drawer).getByRole("button", { name: "Close Event details" }),
    );
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("a start drawer can opt into padding", async () => {
    render(
      <Drawer open onOpenChange={() => undefined} title="Navigation" padded>
        <p>Nav body</p>
      </Drawer>,
    );
    const drawer = await screen.findByRole("dialog", { name: "Navigation" });
    act(() => undefined);
    expect(within(drawer).getByText("Nav body")).toBeVisible();
  });
});

describe("DataTable overlong words (V-01)", () => {
  it("marks only a cell with a word of 40 or more characters", () => {
    const long = "VolumeLongNamedTeam".repeat(10);
    render(
      <DataTable
        caption="People"
        columns={[
          { id: "name", header: "Name", cell: (r) => r.name },
          { id: "note", header: "Note", cell: (r) => r.note },
        ]}
        rows={[{ id: "1", name: long, note: "a short note with many words" }]}
        getRowId={(r) => r.id}
      />,
    );
    expect(screen.getByText(long)).toHaveAttribute("data-longword");
    expect(
      screen.getByText("a short note with many words"),
    ).not.toHaveAttribute("data-longword");
  });
});

describe("DataTable edge cases", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps the given order when the sorted column stops being sortable", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <DataTable
        caption="Things"
        columns={columns}
        rows={rows}
        getRowId={(row) => row.id}
      />,
    );
    await user.click(
      within(screen.getByRole("columnheader", { name: "Name" })).getByRole(
        "button",
      ),
    );
    expect(bodyNames()).toEqual(["alpha", "Beta", "Gamma"]);
    const unsortable = columns.map(
      ({ id, header, cell, rowHeader }): Column<Row> => ({
        id,
        header,
        cell,
        rowHeader,
      }),
    );
    rerender(
      <DataTable
        caption="Things"
        columns={unsortable}
        rows={rows}
        getRowId={(row) => row.id}
      />,
    );
    expect(bodyNames()).toEqual(["Beta", "alpha", "Gamma"]);
    expect(
      screen.getByRole("columnheader", { name: "Name" }),
    ).not.toHaveAttribute("aria-sort");
  });

  it("marks a short cell whose text renders wider than the column (wide scripts)", () => {
    // jsdom has no layout: give ranges a measured width, as a browser does for CJK text.
    const wide = vi.fn(() => ({ width: 400 }) as DOMRect);
    Object.defineProperty(Range.prototype, "getBoundingClientRect", {
      configurable: true,
      value: wide,
    });
    try {
      render(
        <DataTable
          caption="Teams"
          columns={[
            {
              id: "name",
              header: "Name",
              rowHeader: true,
              align: "end",
              cell: (r: { id: string; name: string }) => r.name,
            },
          ]}
          rows={[{ id: "1", name: "数据合同治理团队" }]}
          getRowId={(r) => r.id}
        />,
      );
      const cell = screen.getByRole("rowheader", { name: "数据合同治理团队" });
      expect(cell).toHaveAttribute("data-longword");
      expect(wide).toHaveBeenCalled();
    } finally {
      // Range.prototype has no own rect in jsdom: remove the stub again.
      Reflect.deleteProperty(Range.prototype, "getBoundingClientRect");
    }
  });
});

describe("display helpers", () => {
  it("Identifier shows the whole value with it as the title, capped at the given width", () => {
    render(
      <>
        <Identifier value="urn:dc:contract:payments-ledger-v1" />
        <Identifier value="0b5c3f8e-1a2b" max="12rem" />
      </>,
    );
    const urn = screen.getByText("urn:dc:contract:payments-ledger-v1");
    expect(urn).toHaveAttribute("title", "urn:dc:contract:payments-ledger-v1");
    expect(urn.style.getPropertyValue("--identifier-max")).toBe("18rem");
    expect(
      screen
        .getByText("0b5c3f8e-1a2b")
        .style.getPropertyValue("--identifier-max"),
    ).toBe("12rem");
  });

  it("WhenBy keeps the date and the actor as separate parts", () => {
    render(<WhenBy when="2026-11-01 10:00" by={<span>Ada Admin</span>} />);
    expect(screen.getByText("2026-11-01 10:00")).toBeVisible();
    expect(screen.getByText("Ada Admin").parentElement).toHaveTextContent(
      "by Ada Admin",
    );
  });

  it("Logo pairs the decorative mark with the wordmark, in either tone", () => {
    const { container } = render(
      <>
        <Logo />
        <Logo tone="rail" size={32} />
      </>,
    );
    expect(screen.getAllByText("Contractum")).toHaveLength(2);
    const marks = container.querySelectorAll("svg");
    expect(marks).toHaveLength(2);
    expect(marks[0]).toHaveAttribute("aria-hidden", "true");
    expect(marks[0]).toHaveAttribute("width", "28");
    expect(marks[0]).toHaveClass(logoStyles.light ?? "");
    expect(marks[1]).toHaveAttribute("width", "32");
    expect(marks[1]).toHaveClass(logoStyles.rail ?? "");
  });

  it("Card with a band header puts a titled card's heading in the band, and ignores it without a title", () => {
    render(
      <>
        <Card title="Contracts" header="band">
          <p>Banded body</p>
        </Card>
        <Card header="band">
          <p>Plain body</p>
        </Card>
      </>,
    );
    const banded = screen.getByRole("region", { name: "Contracts" });
    expect(banded).toHaveClass(cardStyles.banded ?? "");
    expect(screen.getByRole("heading", { name: "Contracts" })).toHaveClass(
      cardStyles.band ?? "",
    );
    expect(screen.getByText("Plain body").closest("section")).not.toHaveClass(
      cardStyles.banded ?? "",
    );
  });

  it("NotFound at page level without a back target has an h1 and no breadcrumb trail", async () => {
    renderRouted(() => <NotFound unknownUrl />);
    expect(
      await screen.findByRole("heading", { level: 1, name: "Not found" }),
    ).toBeVisible();
    expect(screen.getByText("This page does not exist.")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Back to/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Folders/ })).toBeNull();
  });
});

describe("router links drawn as buttons and menus with tooltips", () => {
  it("ButtonLink without an icon and IconButtonLink (ghost by default) navigate", async () => {
    const user = userEvent.setup();
    const { router } = renderRouted(() => (
      <>
        <ButtonLink to="/folders" variant="primary" size="small">
          Browse
        </ButtonLink>
        <IconButtonLink
          to="/folders"
          icon={ExternalLink}
          size={32}
          aria-label="Open folders"
        />
      </>
    ));
    const browse = await screen.findByRole("link", { name: "Browse" });
    expect(browse.querySelector("svg")).toBeNull();
    const iconLink = screen.getByRole("link", { name: "Open folders" });
    expect(iconLink).toHaveAttribute("data-button", "icon-32");
    expect(iconLink).toHaveAttribute("href", "/folders");
    await user.click(iconLink);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/folders");
    });
  });

  it("a menu with a trigger tooltip opens to the side and runs the picked item", async () => {
    const user = userEvent.setup();
    const onRename = vi.fn();
    renderRouted(() => (
      <TooltipProvider>
        <Menu
          trigger={
            <button type="button" aria-label="More actions">
              …
            </button>
          }
          tooltip="More actions"
          side="right"
          items={[{ label: "Rename", onSelect: onRename }]}
        />
      </TooltipProvider>
    ));
    const trigger = await screen.findByRole("button", { name: "More actions" });
    await user.hover(trigger);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "More actions",
    );
    await user.click(trigger);
    const menu = await screen.findByRole("menu");
    await user.click(within(menu).getByRole("menuitem", { name: "Rename" }));
    expect(onRename).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(screen.queryByRole("menu")).toBeNull();
    });
  });
});

describe("Dialog with a labelled close control", () => {
  it("focuses the panel, not the close button, and Tab reaches the labelled close", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(
      <TooltipProvider>
        <Dialog
          open
          onOpenChange={onOpenChange}
          title="Contract preview"
          size="full"
          closeIcon={Minimize2}
          closeLabel="Exit full screen"
        >
          <p>Preview body</p>
        </Dialog>
      </TooltipProvider>,
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Contract preview",
    });
    await waitFor(() => {
      expect(dialog).toHaveFocus();
    });
    expect(screen.queryByRole("tooltip")).toBeNull();
    await user.tab();
    const close = within(dialog).getByRole("button", {
      name: "Exit full screen",
    });
    expect(close).toHaveFocus();
    await user.click(close);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("drops the labelled close control when the dialog is not dismissible", async () => {
    render(
      <Dialog
        open
        onOpenChange={() => undefined}
        title="Store it"
        dismissible={false}
        closeLabel="Exit full screen"
      >
        <button type="button">Done</button>
      </Dialog>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Store it" });
    expect(
      within(dialog).queryByRole("button", { name: "Exit full screen" }),
    ).toBeNull();
    // Radix's own auto focus applies: the first focusable control.
    await waitFor(() => {
      expect(
        within(dialog).getByRole("button", { name: "Done" }),
      ).toHaveFocus();
    });
  });
});
