import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { networkProblem, type HttpProblem, type Problem } from "@/api/problem";
import { ProblemAlert } from "@/components/ProblemAlert";

function problem(overrides: Partial<HttpProblem> = {}): HttpProblem {
  return {
    kind: "problem",
    status: 403,
    slug: "csrf-check-failed",
    title: "CSRF check failed",
    detail: "The refresh request was not a same-origin call.",
    errors: [],
    requestId: "r-1",
    ...overrides,
  };
}

function manyErrors(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    pointer: `/field_${String(i + 1)}`,
    detail: `Problem ${String(i + 1)}.`,
  }));
}

// The CopyButton live region is also role status, so pick the Alert by its icon-bearing root.
function alertRegion(): HTMLElement {
  const region = document.querySelector<HTMLElement>(
    '[role="status"][class*="alert"]',
  );
  if (!region) throw new Error("no alert region rendered");
  return region;
}

describe("ProblemAlert", () => {
  it("renders title, detail, slug badge and the request id with its copy button", () => {
    render(<ProblemAlert problem={problem()} />);
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("CSRF check failed")).toBeInTheDocument();
    expect(
      within(alert).getByText(
        "The refresh request was not a same-origin call.",
      ),
    ).toBeInTheDocument();
    expect(within(alert).getByText("csrf-check-failed")).toBeInTheDocument();
    expect(within(alert).getByText("Request ID")).toBeInTheDocument();
    // The request ID is an ID: normal font, not a <code> (quick 261119-r5g, D-03).
    expect(within(alert).getByText("r-1").tagName).toBe("SPAN");
    expect(
      within(alert).getByRole("button", { name: "Copy request ID" }),
    ).toBeInTheDocument();
  });

  it("keeps the slug and request id of a problem the user can fix in collapsed technical details", async () => {
    const actor = userEvent.setup();
    render(
      <ProblemAlert
        context="signin"
        problem={problem({
          status: 401,
          slug: "invalid-credentials",
          title: "Unauthorized",
          detail: null,
        })}
      />,
    );
    expect(screen.getByText("invalid-credentials")).not.toBeVisible();
    expect(screen.getByText("r-1")).not.toBeVisible();
    await actor.click(screen.getByText("Technical details"));
    expect(screen.getByText("invalid-credentials")).toBeVisible();
    expect(screen.getByText("r-1")).toBeVisible();
  });

  it("shows the slug and request id of a server fault up front, without a disclosure", () => {
    render(
      <ProblemAlert
        problem={problem({
          status: 500,
          slug: "internal-error",
          title: "Internal error",
          detail: "Something went wrong.",
        })}
      />,
    );
    expect(screen.getByText("internal-error")).toBeVisible();
    expect(screen.getByText("r-1")).toBeVisible();
    expect(screen.queryByText("Technical details")).toBeNull();
  });

  it("shows only the request id of a server fault without a slug", () => {
    render(
      <ProblemAlert
        problem={problem({ status: 502, slug: null, title: "Bad gateway" })}
      />,
    );
    expect(screen.getByText("r-1")).toBeVisible();
    expect(screen.queryByText("csrf-check-failed")).toBeNull();
    expect(document.querySelector("[data-badge]")).toBeNull();
  });

  it("omits the Request ID group when there is no id", () => {
    render(<ProblemAlert problem={problem({ requestId: null })} />);
    expect(screen.queryByText("Request ID")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Copy request ID" }),
    ).toBeNull();
    expect(screen.getByText("csrf-check-failed")).toBeInTheDocument();
  });

  it("falls back to title and detail for an unknown slug and shows no slug badge", () => {
    render(
      <ProblemAlert
        problem={problem({
          slug: null,
          requestId: null,
          title: "Teapot",
          detail: "Short.",
        })}
      />,
    );
    expect(screen.getByText("Teapot")).toBeInTheDocument();
    expect(screen.getByText("Short.")).toBeInTheDocument();
    expect(screen.queryByText("Request ID")).toBeNull();
  });

  it("renders markup in the detail as text, never as HTML", () => {
    render(
      <ProblemAlert
        problem={problem({
          detail: '<img src=x onerror="boom()"> <b>bold</b>',
        })}
      />,
    );
    expect(screen.getByText(/<img src=x/)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("b")).toBeNull();
  });

  it("shows only the first five of seven errors and reveals the rest on request", async () => {
    const user = userEvent.setup();
    render(
      <ProblemAlert
        problem={problem({
          status: 422,
          slug: "validation-error",
          errors: manyErrors(7),
        })}
      />,
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    const more = screen.getByRole("button", { name: "Show 2 more errors" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    await user.click(more);
    expect(screen.getAllByRole("listitem")).toHaveLength(7);
    expect(screen.getByText("Problem 7.")).toBeInTheDocument();
    const fewer = screen.getByRole("button", { name: "Show fewer errors" });
    expect(fewer).toHaveAttribute("aria-expanded", "true");
    await user.click(fewer);
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
  });

  it("has no disclosure for five errors or fewer and says 1 more error for six", () => {
    const { rerender } = render(
      <ProblemAlert problem={problem({ errors: manyErrors(5) })} />,
    );
    expect(screen.queryByRole("button", { name: /more error/ })).toBeNull();
    rerender(<ProblemAlert problem={problem({ errors: manyErrors(6) })} />);
    expect(
      screen.getByRole("button", { name: "Show 1 more error" }),
    ).toBeInTheDocument();
  });

  it("shows a mapped pointer as its field label and an unmapped one in monospace", () => {
    render(
      <ProblemAlert
        problem={problem({
          errors: [
            { pointer: "/new_password", detail: "Too common." },
            { pointer: "/mystery", detail: "Not allowed." },
          ],
        })}
        fieldLabels={{ "/new_password": "New password" }}
      />,
    );
    expect(screen.getByText("New password")).toBeInTheDocument();
    expect(screen.queryByText("/new_password")).toBeNull();
    const pointer = screen.getByText("/mystery");
    expect(pointer.tagName).toBe("CODE");
  });

  it("names a whole-request error (empty pointer) as the request", () => {
    render(
      <ProblemAlert
        problem={problem({
          errors: [{ pointer: "", detail: "Body must be an object." }],
        })}
      />,
    );
    const label = screen.getByText("(request)");
    expect(label.tagName).toBe("CODE");
    expect(label.closest("li")).toHaveTextContent(
      "(request) Body must be an object.",
    );
  });

  it("renders the network warning with Retry loading", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(<ProblemAlert problem={networkProblem()} onRetry={onRetry} />);
    const alert = alertRegion();
    expect(
      within(alert).getByText("Can't reach the server"),
    ).toBeInTheDocument();
    expect(
      within(alert).getByText("Check your connection and try again."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Request ID")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry loading" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders a 503 as a warning with the fixed copy and the request id", () => {
    render(
      <ProblemAlert
        problem={problem({
          status: 503,
          slug: "service-unavailable",
          title: "Identity is down",
          detail: "upstream connect error",
        })}
        onRetry={() => undefined}
      />,
    );
    const alert = alertRegion();
    expect(
      within(alert).getByText("Service temporarily unavailable"),
    ).toBeInTheDocument();
    expect(within(alert).queryByText("upstream connect error")).toBeNull();
    expect(within(alert).getByText("service-unavailable")).toBeInTheDocument();
    expect(
      within(alert).getByRole("button", { name: "Retry loading" }),
    ).toBeInTheDocument();
  });

  it("renders the 403 action copy plus the slug", () => {
    render(
      <ProblemAlert
        context="action"
        problem={problem({
          slug: "forbidden",
          title: "Forbidden",
          detail: null,
        })}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(
      within(alert).getByText(
        "You are not permitted to do this here. Ask a folder admin for access.",
      ),
    ).toBeInTheDocument();
    expect(within(alert).getByText("forbidden")).toBeInTheDocument();
  });

  it("renders the sign-in 401 copy as a danger alert and the throttle copy as a warning", () => {
    const { rerender } = render(
      <ProblemAlert
        context="signin"
        problem={problem({
          status: 401,
          slug: "invalid-credentials",
          title: "Unauthorized",
          detail: "no such user",
        })}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Username or password is incorrect.",
    );
    expect(screen.queryByText("no such user")).toBeNull();
    rerender(
      <ProblemAlert
        context="signin"
        problem={problem({ status: 429, slug: "too-many-attempts" })}
      />,
    );
    expect(alertRegion()).toHaveTextContent(
      "Too many attempts, try again in a moment.",
    );
  });

  it("has no Retry loading action without onRetry", () => {
    const p: Problem = networkProblem();
    render(<ProblemAlert problem={p} />);
    expect(screen.queryByRole("button", { name: "Retry loading" })).toBeNull();
  });
});
