// MonacoDiff names both inputs (axe aria-input-field-name): the diff editor is created with
// originalAriaLabel/modifiedAriaLabel, and nameDiffSide restores a side's name whenever Monaco's
// diff editor resets it to "" on a later options update. The Monaco React wrapper and the Monaco
// setup are mocked at the module boundary.
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const ARIA_LABEL = 8;

vi.mock("@monaco-editor/react", () => ({
  default: () => null,
  DiffEditor: (props: { options?: Record<string, unknown> }) => (
    <div data-testid="diff" data-options={JSON.stringify(props.options)} />
  ),
}));
vi.mock("./monacoSetup", () => ({
  editorTheme: () => "dc-light",
  monaco: {
    MarkerSeverity: { Error: 8, Warning: 4, Info: 2 },
    editor: { EditorOption: { ariaLabel: 8 } },
  },
}));

function fakeSide(initial: string) {
  let label = initial;
  const listeners: (() => void)[] = [];
  const side = {
    getOption: vi.fn((id: number) => (id === ARIA_LABEL ? label : undefined)),
    updateOptions: vi.fn((options: { ariaLabel?: string }) => {
      if (options.ariaLabel !== undefined) label = options.ariaLabel;
      for (const listener of listeners) listener();
    }),
    onDidChangeConfiguration: vi.fn((listener: () => void) => {
      listeners.push(listener);
      return { dispose: () => undefined };
    }),
  };
  return {
    side,
    label: () => label,
    /** What Monaco's diff editor does on a diff-level update without the aria options. */
    reset: () => {
      label = "";
      for (const listener of listeners) listener();
    },
  };
}

describe("MonacoDiff", () => {
  it("creates the diff editor with a name for each side", async () => {
    const { MonacoDiff } = await import("./MonacoEditor");
    render(<MonacoDiff original="a: 1" modified="a: 2" language="yaml" />);
    const options = JSON.parse(
      screen.getByTestId("diff").getAttribute("data-options") ?? "{}",
    ) as Record<string, unknown>;
    expect(options).toMatchObject({
      originalAriaLabel: "Before",
      modifiedAriaLabel: "After",
      readOnly: true,
    });
  });

  it("restores a side's name after the diff editor clears it", async () => {
    const { nameDiffSide } = await import("./MonacoEditor");
    const fake = fakeSide("");
    nameDiffSide(fake.side as never, "Before");
    expect(fake.label()).toBe("Before");
    fake.reset();
    expect(fake.label()).toBe("Before");
    // A name already in place is left alone.
    const named = fakeSide("Server copy");
    nameDiffSide(named.side as never, "Before");
    expect(named.side.updateOptions).not.toHaveBeenCalled();
    expect(named.label()).toBe("Server copy");
  });
});
