import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderApp, ROOT_ID } from "@/test/renderApp";

describe("createAppRouter", () => {
  it("renders the not-found page for a URL outside the route tree", async () => {
    renderApp("/no-such-area/at-all");
    expect(
      await screen.findByText("This page does not exist."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "This item does not exist, or you do not have access to it.",
      ),
    ).not.toBeInTheDocument();
  });

  it("keeps the app shell around an unknown page below a known area", async () => {
    renderApp(`/folders/${ROOT_ID}/no-such-tab`);
    expect(
      await screen.findByText("This page does not exist."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Primary" }),
    ).toBeInTheDocument();
  });

  it("renders a URL outside every area without the shell", async () => {
    renderApp("/no-such-area");
    await screen.findByText("This page does not exist.");
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
  });
});
