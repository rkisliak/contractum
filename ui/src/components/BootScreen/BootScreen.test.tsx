import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BootScreen } from "./index";

describe("BootScreen", () => {
  it("shows the text mark and a Loading status region while config loads", () => {
    render(<BootScreen state="loading" />);
    expect(
      screen.getByRole("heading", { level: 1, name: "Contractum" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("defaults to the loading state", () => {
    render(<BootScreen />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
  });

  it("explains the failure and offers Try loading again", async () => {
    const onRetry = vi.fn();
    render(<BootScreen state="failed" onRetry={onRetry} />);
    expect(screen.getByRole("main")).toHaveTextContent("Contractum");
    expect(
      screen.getByRole("heading", { level: 1, name: "Can't reach the server" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Check your connection and try again."),
    ).toBeInTheDocument();
    // Only the loading state is a status region; the failure is the whole page.
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Try loading again" }),
    );
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("keeps the failure icon out of the accessibility tree", () => {
    const { container } = render(<BootScreen state="failed" />);
    expect(container.querySelector("svg")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });
});
