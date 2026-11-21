import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import userEvent from "@testing-library/user-event";
import { Building2, User } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Alert, type AlertIntent } from "@/components/Alert";
import alertStyles from "@/components/Alert/Alert.module.css";
import { Avatar } from "@/components/Avatar";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { StatusBadge, statusBadges } from "@/components/Badge/status";
import { CopyButton } from "@/components/CopyButton";
import { EmptyState, Skeleton, Spinner } from "@/components/states";

describe("Alert", () => {
  it("uses role alert for danger", () => {
    render(<Alert intent="danger">Could not save.</Alert>);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not save.");
  });

  it.each<AlertIntent>(["info", "success", "warning", "neutral"])(
    "uses role status for %s",
    (intent) => {
      render(<Alert intent={intent}>Something happened.</Alert>);
      expect(screen.getByRole("status")).toHaveTextContent(
        "Something happened.",
      );
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );

  it.each<[AlertIntent, string]>([
    ["info", "Information"],
    ["success", "Success"],
    ["warning", "Warning"],
    ["danger", "Error"],
    ["neutral", "Note"],
  ])("states the %s intent in text and hides the icon", (intent, prefix) => {
    const { container } = render(<Alert intent={intent}>Body</Alert>);
    expect(
      screen.getByRole(intent === "danger" ? "alert" : "status"),
    ).toHaveTextContent(`${prefix}: Body`);
    const icon = container.querySelector("svg");
    expect(icon).not.toBeNull();
    expect(icon).toHaveAttribute("aria-hidden", "true");
  });

  it("renders the title and the action row", () => {
    render(
      <Alert
        intent="warning"
        title="Heads up"
        action={<button>Retry loading</button>}
      >
        Details
      </Alert>,
    );
    expect(screen.getByText("Heads up")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Retry loading" }),
    ).toBeInTheDocument();
  });

  it("renders a title-only alert without an empty body block", () => {
    render(<Alert intent="success" title="Saved" />);
    const alert = screen.getByRole("status");
    expect(alert).toHaveTextContent("Success: Saved");
    expect(alert.querySelector(`.${alertStyles.body ?? "body"}`)).toBeNull();
  });

  it("offers a Dismiss alert button only when onDismiss is given", async () => {
    const onDismiss = vi.fn();
    const { rerender } = render(<Alert intent="info">Body</Alert>);
    expect(screen.queryByRole("button", { name: "Dismiss alert" })).toBeNull();

    rerender(
      <Alert intent="info" onDismiss={onDismiss}>
        Body
      </Alert>,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Dismiss alert" }),
    );
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("gives the dismiss button a hit area of at least 24x24", () => {
    render(
      <Alert intent="info" onDismiss={() => undefined}>
        Body
      </Alert>,
    );
    // jsdom does no layout, so the size is asserted through the IconButton size class contract:
    // the 24 size class is the floor and no smaller class exists (C-09).
    const button = screen.getByRole("button", { name: "Dismiss alert" });
    expect(button.className).toMatch(/icon24/);
  });
});

describe("Badge and StatusBadge", () => {
  it("always renders the label text", () => {
    render(<Badge intent="neutral">Archived</Badge>);
    expect(screen.getByText("Archived")).toBeInTheDocument();
  });

  it("gives a long label its full text as the title (it is cut with an ellipsis, volume audit V-05)", () => {
    const long = `${"contract-name-".repeat(20)} 1.0.0`;
    render(<Badge intent="neutral">{long}</Badge>);
    expect(screen.getByText(long)).toHaveAttribute("title", long);
  });

  it("gives no title to a label that is not plain text", () => {
    render(
      <Badge intent="neutral">
        <span>v1.2.0</span>
      </Badge>,
    );
    expect(screen.getByText("v1.2.0").parentElement).not.toHaveAttribute(
      "title",
    );
  });

  it("maps entra to brand with Building2 and local to neutral with User", () => {
    expect(statusBadges.entra.intent).toBe("brand");
    expect(statusBadges.entra.icon).toBe(Building2);
    expect(statusBadges.local.intent).toBe("neutral");
    expect(statusBadges.local.icon).toBe(User);
  });

  it("renders the status label with a decorative icon", () => {
    const { container } = render(<StatusBadge status="entra" />);
    expect(screen.getByText("Microsoft Entra")).toBeInTheDocument();
    expect(container.querySelector("svg")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    expect(container.querySelector("svg")).toHaveAttribute("width", "14");
  });

  it("renders the local status label", () => {
    render(<StatusBadge status="local" />);
    expect(screen.getByText("Local account")).toBeInTheDocument();
  });
});

describe("Avatar", () => {
  it.each([
    ["Ada Lovelace", "AL"],
    ["admin", "A"],
    ["  grace   brewster   hopper ", "GB"],
    ["", "?"],
    ["   ", "?"],
  ])("shows the initials of %j as %s", (name, initials) => {
    const { container } = render(<Avatar name={name} />);
    const node = container.firstElementChild;
    expect(node).toHaveTextContent(initials);
    expect(node).toHaveAttribute("aria-hidden", "true");
  });
});

describe("CopyButton", () => {
  const secret = "dcp_abcdef_super-secret-value";
  let writeText: ReturnType<typeof vi.fn<(text: string) => Promise<void>>>;

  beforeEach(() => {
    vi.useFakeTimers();
    writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function setup(onError?: (error: unknown) => void) {
    // Fake timers: userEvent awaits real timers under Testing Library, so clicks use fireEvent.
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    const view = render(
      <CopyButton
        value={secret}
        label="Copy request ID"
        copiedLabel="Request ID copied"
        onError={onError}
      />,
    );
    async function click() {
      await act(() => {
        fireEvent.click(
          screen.getByRole("button", { name: "Copy request ID" }),
        );
        // Returning a promise lets act flush the clipboard promise before assertions.
        return Promise.resolve();
      });
    }
    return { click, ...view };
  }

  it("copies, confirms in the label and a polite region, then reverts after 3 s", async () => {
    const { click } = setup();
    await click();

    expect(writeText).toHaveBeenCalledWith(secret);
    expect(
      screen.getByRole("button", { name: "Request ID copied" }),
    ).toBeInTheDocument();
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("Request ID copied");

    act(() => {
      vi.advanceTimersByTime(2999);
    });
    expect(
      screen.getByRole("button", { name: "Request ID copied" }),
    ).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(
      screen.getByRole("button", { name: "Copy request ID" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("never puts the copied value in the live region or the accessible tree text", async () => {
    const { click, container } = setup();
    await click();
    expect(
      screen.getByRole("button", { name: "Request ID copied" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).not.toHaveTextContent(secret);
    expect(container).not.toHaveTextContent(secret);
  });

  it("keeps the label and calls onError when the clipboard rejects", async () => {
    const failure = new Error("denied");
    writeText.mockRejectedValue(failure);
    const onError = vi.fn();
    const { click } = setup(onError);
    await click();

    expect(onError).toHaveBeenCalledWith(failure);
    expect(
      screen.getByRole("button", { name: "Copy request ID" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });
});

function stubReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        matches: reduce && query.includes("prefers-reduced-motion"),
        media: query,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
      }) as unknown as MediaQueryList,
  );
}

describe("Skeleton", () => {
  it("is a busy region with a hidden Loading status naming the thing", () => {
    const { container } = render(<Skeleton label="access" rows={3} />);
    const region = container.firstElementChild;
    expect(region).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status")).toHaveTextContent("Loading access");
    expect(region?.querySelectorAll("[data-skeleton-row]")).toHaveLength(3);
  });

  it("defaults to one row", () => {
    const { container } = render(<Skeleton label="contracts" />);
    expect(container.querySelectorAll("[data-skeleton-row]")).toHaveLength(1);
  });
});

describe("Spinner", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders a rotating decorative icon with a status label", () => {
    stubReducedMotion(false);
    const { container } = render(<Spinner />);
    expect(container.querySelector("svg")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
  });

  it("becomes the static text Loading… under prefers-reduced-motion", () => {
    stubReducedMotion(true);
    const { container } = render(<Spinner />);
    expect(container.querySelector("svg")).toBeNull();
    expect(screen.getByText("Loading…")).toBeVisible();
  });

  it("renders the animated spinner on the server, where no media query applies", () => {
    stubReducedMotion(true);
    const html = renderToString(<Spinner label="Saving…" />);
    expect(html).toContain("<svg");
    expect(html).toContain('role="status"');
    expect(html).toContain("Saving…");
  });

  it("uses a custom label under reduced motion", () => {
    stubReducedMotion(true);
    render(<Spinner label="Saving…" />);
    expect(screen.getByText("Saving…")).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("renders an h2 by default, the sentence and no action", () => {
    render(
      <EmptyState
        heading="No folder access yet"
        body="Ask a folder admin to grant you access."
      />,
    );
    expect(
      screen.getByRole("heading", { level: 2, name: "No folder access yet" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Ask a folder admin to grant you access."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders the heading at the given level and the action when provided", () => {
    render(
      <EmptyState
        heading="No contracts yet"
        body="Published contracts appear here."
        level={3}
        action={<button>Create folder</button>}
      />,
    );
    expect(
      screen.getByRole("heading", { level: 3, name: "No contracts yet" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Create folder" }),
    ).toBeInTheDocument();
  });

  it("hides the optional icon from assistive technology", () => {
    const { container } = render(
      <EmptyState
        heading="No rule sets yet"
        body="Rule sets appear here."
        icon={Building2}
      />,
    );
    expect(container.querySelector("svg")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });
});

describe("Button with a long text label (V-11)", () => {
  it("shows a long label in a text span with the full text as its title", () => {
    const long = `Deactivate ${"VolumeEntraUser".repeat(14)}`;
    render(
      <Button variant="danger" size="small">
        {long}
      </Button>,
    );
    const button = screen.getByRole("button", { name: long });
    expect(within(button).getByText(long)).toHaveAttribute("title", long);
  });
});
