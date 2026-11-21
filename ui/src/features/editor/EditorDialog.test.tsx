// The full-screen editor: "Expand editor" opens it, it shows the caller's title, toolbar and notices
// above the page editor's buffer (a skeleton while the document loads) and "Exit full screen"
// closes it.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { TooltipProvider } from "@/components/Tooltip";
import { EditorDialog, ExpandEditorButton } from "./EditorDialog";

function Harness({ loaded }: { loaded: boolean }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("a: 1");
  return (
    <TooltipProvider>
      <ExpandEditorButton
        onClick={() => {
          setOpen(true);
        }}
      />
      <EditorDialog
        open={open}
        onOpenChange={setOpen}
        title="Orders · Document"
        toolbar={<button type="button">Download</button>}
        notices={<p>Saved revision 3</p>}
        editor={
          loaded
            ? {
                value: text,
                onChange: setText,
                language: "yaml",
                label: "Document",
              }
            : null
        }
      />
    </TooltipProvider>
  );
}

describe("EditorDialog", () => {
  it("opens over the page with the toolbar, notices and the shared buffer", async () => {
    const user = userEvent.setup();
    render(<Harness loaded />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand editor" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Orders · Document",
    });
    expect(dialog).toContainElement(
      screen.getByRole("button", { name: "Download" }),
    );
    expect(dialog).toHaveTextContent("Saved revision 3");
    const editor = screen.getByRole("textbox", { name: "Document" });
    expect(editor).toHaveAttribute("data-fit", "container");
    await user.type(editor, "\nb: 2");
    expect(editor).toHaveValue("a: 1\nb: 2");
    await user.click(screen.getByRole("button", { name: "Exit full screen" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows a skeleton until the document has loaded", async () => {
    const user = userEvent.setup();
    render(<Harness loaded={false} />);
    await user.click(screen.getByRole("button", { name: "Expand editor" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText("Loading document")).toBeInTheDocument();
  });
});
