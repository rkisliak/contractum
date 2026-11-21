import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Eye, Trash2 } from "lucide-react";
import { describe, expect, it, vi } from "vitest";
import { Button, IconButton } from "@/components/Button";
import { Card } from "@/components/Card";
import { Menu } from "@/components/Menu";
import { SegmentedControl } from "@/components/SegmentedControl";
import { TooltipProvider } from "@/components/Tooltip";
import {
  PasswordField,
  SelectField,
  TextField,
  useFormFieldControl,
} from "@/components/form";
import formStyles from "@/components/form/form.module.css";
import { TextLink } from "@/components/TextLink";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import textLinkStyles from "@/components/TextLink/TextLink.module.css";
import hiddenStyles from "@/components/VisuallyHidden/VisuallyHidden.module.css";

function SignInCard({ onSubmit }: { onSubmit: () => void }) {
  return (
    <Card title="Sign in" titleAs="h1">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <TextField id="username" label="Username" autoComplete="username" />
        <PasswordField
          id="password"
          label="Password"
          autoComplete="current-password"
        />
        <Button variant="primary" size="large" type="submit" fullWidth>
          Sign in
        </Button>
      </form>
    </Card>
  );
}

describe("sign-in card building blocks", () => {
  it("labels both inputs and submits the form with Enter in the password field", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(<SignInCard onSubmit={onSubmit} />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Sign in" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Sign in" })).toBeInTheDocument();
    const username = screen.getByLabelText("Username");
    const password = screen.getByLabelText("Password");
    expect(username).toHaveAttribute("autocomplete", "username");
    expect(password).toHaveAttribute("autocomplete", "current-password");

    await user.type(username, "ada");
    await user.type(password, "pw{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("toggles the password reveal with aria-pressed and the Show / Hide labels", async () => {
    const user = userEvent.setup();
    render(
      <PasswordField
        id="password"
        label="Password"
        autoComplete="current-password"
      />,
    );
    const input = screen.getByLabelText("Password");
    expect(input).toHaveAttribute("type", "password");

    const show = screen.getByRole("button", { name: "Show password" });
    expect(show).toHaveAttribute("aria-pressed", "false");
    await user.click(show);

    expect(input).toHaveAttribute("type", "text");
    const hide = screen.getByRole("button", { name: "Hide password" });
    expect(hide).toHaveAttribute("aria-pressed", "true");
    await user.click(hide);
    expect(input).toHaveAttribute("type", "password");
    expect(
      screen.getByRole("button", { name: "Show password" }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("allows paste into the password field", async () => {
    const user = userEvent.setup();
    render(
      <PasswordField
        id="password"
        label="Password"
        autoComplete="current-password"
      />,
    );
    await user.click(screen.getByLabelText("Password"));
    await user.paste("pasted-secret");
    expect(screen.getByLabelText("Password")).toHaveValue("pasted-secret");
  });

  it("wires an error to the control with aria-invalid and aria-describedby", () => {
    render(
      <TextField
        id="username"
        label="Username"
        helper="The name you were given."
        error="Enter your username."
        autoComplete="username"
      />,
    );
    const input = screen.getByLabelText("Username");
    expect(input).toHaveAttribute("aria-invalid", "true");
    const error = screen.getByText("Enter your username.");
    const helper = screen.getByText("The name you were given.");
    const describedBy = input.getAttribute("aria-describedby") ?? "";
    expect(describedBy.split(" ")).toContain(error.closest("p")?.id);
    expect(describedBy.split(" ")).toContain(helper.id);
    expect(error).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your username.");
  });

  it("has no aria-invalid and no describedby on a clean field", () => {
    render(<TextField id="u" label="Username" autoComplete="username" />);
    const input = screen.getByLabelText("Username");
    expect(input).not.toHaveAttribute("aria-invalid");
    expect(input).not.toHaveAttribute("aria-describedby");
  });

  it("marks an optional field in its label and keeps long values and messages intact", async () => {
    const user = userEvent.setup();
    const long = "x".repeat(300);
    render(
      <TextField
        id="note"
        label="Note"
        optional
        error={`Too long: ${long}`}
        autoComplete="off"
      />,
    );
    expect(screen.getByText("Optional")).toBeInTheDocument();
    expect(screen.getByText(`Too long: ${long}`)).toBeInTheDocument();
    const inner = screen.getByLabelText(/^Note/);
    await user.type(inner, long);
    expect(inner).toHaveValue(long);
  });

  it("requires autoComplete on TextField and PasswordField by type", () => {
    // @ts-expect-error autoComplete is required
    render(<TextField id="a" label="A" />);
    // @ts-expect-error autoComplete is required
    render(<PasswordField id="b" label="B" />);
    // The type error is the contract; at run time both fields still render and are labelled.
    expect(screen.getByLabelText("A")).toBeInTheDocument();
    expect(screen.getByLabelText("B")).toBeInTheDocument();
  });
});

describe("field frame variants", () => {
  it("draws a small text control and keeps a hidden select label for assistive technology", () => {
    render(
      <>
        <TextField
          id="filter"
          label="Filter"
          autoComplete="off"
          controlSize="small"
        />
        <SelectField
          id="switch-folder"
          label="Switch folder"
          labelHidden
          controlSize="small"
          value="a"
          onChange={() => undefined}
          options={[
            { value: "a", label: "Payments" },
            { value: "b", label: "Ledger" },
          ]}
        />
      </>,
    );
    const filter = screen.getByRole("textbox", { name: "Filter" });
    expect(filter.parentElement).toHaveClass(formStyles.shellSmall ?? "");
    const select = screen.getByRole("combobox", { name: "Switch folder" });
    expect(screen.getByText("Switch folder")).toHaveClass(
      formStyles.labelHidden ?? "",
    );
    // Without a placeholder there is no empty first choice.
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Payments", "Ledger"]);
  });

  it("refuses a control rendered outside a FormField", () => {
    function Orphan() {
      const wiring = useFormFieldControl();
      return <input id={wiring.id} />;
    }
    // React reports the render error on the console as well; keep the output clean.
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    try {
      expect(() => render(<Orphan />)).toThrow(
        "A form control must be rendered inside a FormField",
      );
    } finally {
      consoleError.mockRestore();
    }
  });
});

describe("Button", () => {
  it("shows a spinner in place of the label when busy without a busy label", () => {
    render(
      <Button variant="primary" size="small" busy>
        Save
      </Button>,
    );
    const button = screen.getByRole("button");
    expect(button).toHaveAttribute("aria-busy", "true");
    // The label gives way to a decorative spinning icon; the width stays in the same grid cell.
    expect(button.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(button).not.toHaveTextContent("Save");
  });

  it("shows the busy label, sets aria-busy, ignores clicks and stays focusable", async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(
      <Button
        variant="primary"
        size="large"
        busy
        busyLabel="Signing in…"
        onClick={onClick}
      >
        Sign in
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Signing in…" });
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toBeEnabled();
    await user.click(button);
    expect(onClick).not.toHaveBeenCalled();
    button.focus();
    expect(button).toHaveFocus();
  });

  it("does not submit a form while busy", async () => {
    const onSubmit = vi.fn((event: { preventDefault: () => void }) => {
      event.preventDefault();
    });
    const user = userEvent.setup();
    render(
      <form onSubmit={onSubmit}>
        <Button
          variant="primary"
          size="large"
          type="submit"
          busy
          busyLabel="Signing in…"
        >
          Sign in
        </Button>
      </form>,
    );
    await user.click(screen.getByRole("button"));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps the normal label name and fires onClick when not busy", async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(
      <Button
        variant="secondary"
        size="small"
        busyLabel="Saving…"
        onClick={onClick}
      >
        Save
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Save" });
    expect(button).not.toHaveAttribute("aria-busy");
    await user.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("renders every variant and size as a native button defaulting to type button", () => {
    const variants = [
      "primary",
      "secondary",
      "danger",
      "ghost",
      "outline-neutral",
      "outline-brand",
      "outline-danger",
    ] as const;
    render(
      <div>
        {variants.flatMap((variant) =>
          (["small", "large"] as const).map((size) => (
            <Button key={`${variant}-${size}`} variant={variant} size={size}>
              {`${variant} ${size}`}
            </Button>
          )),
        )}
      </div>,
    );
    expect(screen.getAllByRole("button")).toHaveLength(14);
    for (const button of screen.getAllByRole("button")) {
      expect(button).toHaveAttribute("type", "button");
    }
  });

  it("does not fire onClick when disabled", async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(
      <Button variant="danger" size="small" disabled onClick={onClick}>
        Unpublish version
      </Button>,
    );
    await user.click(screen.getByRole("button"));
    expect(onClick).not.toHaveBeenCalled();
  });
});

describe("IconButton", () => {
  it("uses the aria-label as its accessible name", () => {
    render(<IconButton size={36} icon={Eye} aria-label="Show details" />);
    expect(
      screen.getByRole("button", { name: "Show details" }),
    ).toBeInTheDocument();
  });

  it("fails type checking without an aria-label", () => {
    // @ts-expect-error aria-label is required
    render(<IconButton size={32} icon={Eye} />);
    // Without the label the button has no accessible name, which is why the prop is required.
    expect(screen.getByRole("button")).not.toHaveAccessibleName();
  });
});

describe("VisuallyHidden", () => {
  it("keeps its content in the accessibility tree", () => {
    render(
      <button type="button">
        Save <VisuallyHidden>(draft)</VisuallyHidden>
      </button>,
    );
    expect(
      screen.getByRole("button", { name: "Save (draft)" }),
    ).toBeInTheDocument();
    expect(screen.getByText("(draft)")).toHaveClass(hiddenStyles.root ?? "");
  });

  it("uses the clip pattern and never display none", () => {
    const css = readFileSync(
      resolve(import.meta.dirname, "VisuallyHidden/VisuallyHidden.module.css"),
      "utf8",
    );
    expect(css).not.toMatch(/display:\s*none/);
    expect(css).not.toMatch(/visibility:\s*hidden/);
    expect(css).toMatch(/clip-path:\s*inset\(50%\)/);
    expect(css).toMatch(/position:\s*absolute/);
  });
});

async function renderInRouter(ui: React.ReactNode) {
  const rootRoute = createRootRoute({ component: () => <>{ui}</> });
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(<RouterProvider router={router} />);
  await router.load();
}

describe("TextLink", () => {
  it("underlines inline links and leaves nav links bare", () => {
    render(
      <>
        <TextLink href="/docs">Inline</TextLink>
        <TextLink href="/home" variant="nav">
          Nav
        </TextLink>
      </>,
    );
    const inline = screen.getByRole("link", { name: "Inline" });
    const nav = screen.getByRole("link", { name: "Nav" });
    expect(inline).toHaveClass(textLinkStyles.inline ?? "");
    expect(nav).not.toHaveClass(textLinkStyles.inline ?? "");
    expect(nav).toHaveClass(textLinkStyles.nav ?? "");
    expect(inline).toHaveAttribute("data-variant", "inline");
    expect(nav).toHaveAttribute("data-variant", "nav");
    const css = readFileSync(
      resolve(import.meta.dirname, "TextLink/TextLink.module.css"),
      "utf8",
    );
    expect(css).toMatch(/\.inline\s*{[^}]*text-decoration:\s*underline/);
    expect(css).not.toMatch(/\.nav\s*{[^}]*text-decoration:\s*underline/);
  });

  it("opens external links in a new tab with noopener noreferrer and hidden text", () => {
    render(
      <TextLink href="https://example.com/docs" external>
        API docs
      </TextLink>,
    );
    const link = screen.getByRole("link", {
      name: "API docs (opens in a new tab)",
    });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("href", "https://example.com/docs");
    expect(link.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("does not open internal href links in a new tab", () => {
    render(<TextLink href="/docs">Docs</TextLink>);
    const link = screen.getByRole("link", { name: "Docs" });
    expect(link).not.toHaveAttribute("target");
    expect(link).not.toHaveAttribute("rel");
  });

  it("renders a router Link when `to` is set", async () => {
    await renderInRouter(<TextLink to="/">Home</TextLink>);
    const link = await screen.findByRole("link", { name: "Home" });
    expect(link).toHaveAttribute("href", "/");
    expect(link).toHaveClass(textLinkStyles.inline ?? "");
  });
});

describe("Menu item with an unavailableReason", () => {
  function renderMenu(onSelect: () => void, onOther: () => void) {
    render(
      <TooltipProvider>
        <Menu
          trigger={
            <Button variant="secondary" size="small">
              Open
            </Button>
          }
          items={[
            { label: "Rename", onSelect: onOther },
            {
              label: "Delete folder…",
              icon: Trash2,
              danger: true,
              unavailableReason: "Not while the folder holds contracts.",
              onSelect,
            },
          ]}
        />
      </TooltipProvider>,
    );
  }

  it("is aria-disabled, describes its reason, stays focusable and keeps the menu open", async () => {
    const onSelect = vi.fn();
    const onOther = vi.fn();
    renderMenu(onSelect, onOther);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Open" }));
    const item = await screen.findByRole("menuitem", {
      name: /Delete folder…/,
    });
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(item).not.toHaveAttribute("data-disabled");
    expect(item).toHaveAccessibleDescription(
      "Not while the folder holds contracts.",
    );
    // A Radix-disabled item is skipped by the arrow keys; this one is reachable.
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{ArrowDown}");
    expect(item).toHaveFocus();
    await user.keyboard("{Enter}");
    await user.click(item);
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByRole("menu")).toBeVisible();
    expect(onOther).not.toHaveBeenCalled();
  });
});

describe("SegmentedControl", () => {
  it("is a labelled group of toggle buttons with the current one pressed", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <SegmentedControl
        label="Source"
        value="local"
        options={[
          { value: "all", label: "All" },
          { value: "local", label: "Local" },
          { value: "entra", label: "Entra" },
        ]}
        onChange={onChange}
      />,
    );
    const group = screen.getByRole("group", { name: "Source" });
    const buttons = within(group).getAllByRole("button");
    expect(buttons.map((button) => button.textContent)).toEqual([
      "All",
      "Local",
      "Entra",
    ]);
    expect(
      buttons.map((button) => button.getAttribute("aria-pressed")),
    ).toEqual(["false", "true", "false"]);
    // Each one is a shared Button, so the layout rules see it as one.
    expect(buttons.every((button) => button.hasAttribute("data-button"))).toBe(
      true,
    );
    await user.click(within(group).getByRole("button", { name: "Entra" }));
    expect(onChange).toHaveBeenCalledWith("entra");
  });
});
