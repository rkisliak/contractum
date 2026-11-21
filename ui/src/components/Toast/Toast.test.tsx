import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test } from "vitest";
import { ToastProvider, useToast } from "./index";

function Fire({ count, description }: { count: number; description?: string }) {
  const toast = useToast();
  return (
    <button
      type="button"
      onClick={() => {
        for (let i = 1; i <= count; i++) {
          toast({
            title: `Toast ${String(i)}`,
            ...(description === undefined ? {} : { description }),
          });
        }
      }}
    >
      Fire
    </button>
  );
}

// Radix Toast's swipe handling calls pointer-capture methods jsdom does not implement.
Object.assign(Element.prototype, {
  hasPointerCapture: () => false,
  releasePointerCapture: () => undefined,
  setPointerCapture: () => undefined,
});

describe("Toast", () => {
  test("shows a toast in the Notifications region and dismisses it", async () => {
    const u = userEvent.setup();
    render(
      <ToastProvider>
        <Fire count={1} />
      </ToastProvider>,
    );
    await u.click(screen.getByRole("button", { name: "Fire" }));
    expect(await screen.findByText("Toast 1")).toBeVisible();
    expect(
      screen.getByRole("region", { name: /Notifications/ }),
    ).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: "Dismiss notification" }));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(screen.queryByText("Toast 1")).not.toBeInTheDocument();
  });

  test("keeps at most three visible, the oldest leaving first, and wraps long text", async () => {
    const u = userEvent.setup();
    const long = "word".repeat(75);
    render(
      <ToastProvider>
        <Fire count={5} description={long} />
      </ToastProvider>,
    );
    await u.click(screen.getByRole("button", { name: "Fire" }));
    expect(await screen.findByText("Toast 5")).toBeVisible();
    expect(screen.getByText("Toast 4")).toBeVisible();
    expect(screen.getByText("Toast 3")).toBeVisible();
    expect(screen.queryByText("Toast 2")).not.toBeInTheDocument();
    expect(screen.queryByText("Toast 1")).not.toBeInTheDocument();
    expect(screen.getAllByText(long)).toHaveLength(3);
  });

  test("shows a neutral note and closes only the toast that is dismissed", async () => {
    const u = userEvent.setup();
    function Mixed() {
      const toast = useToast();
      return (
        <button
          type="button"
          onClick={() => {
            toast({ title: "Saved" });
            toast({ title: "Nothing to change", intent: "neutral" });
          }}
        >
          Fire
        </button>
      );
    }
    render(
      <ToastProvider>
        <Mixed />
      </ToastProvider>,
    );
    await u.click(screen.getByRole("button", { name: "Fire" }));
    const note = await screen.findByText("Nothing to change");
    expect(note).toHaveTextContent("Note: Nothing to change");
    expect(screen.getByText("Saved")).toHaveTextContent("Success: Saved");
    const [, noteDismiss] = screen.getAllByRole("button", {
      name: "Dismiss notification",
    });
    if (noteDismiss === undefined) throw new Error("two toasts expected");
    await u.click(noteDismiss);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(screen.queryByText("Nothing to change")).not.toBeInTheDocument();
    expect(screen.getByText("Saved")).toBeVisible();
  });

  test("is a silent no-op outside the provider", async () => {
    const u = userEvent.setup();
    render(<Fire count={1} />);
    await u.click(screen.getByRole("button", { name: "Fire" }));
    expect(screen.queryByText("Toast 1")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Notifications/ })).toBeNull();
  });
});
