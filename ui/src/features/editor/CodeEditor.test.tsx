// CodeEditor and DiffView: the plain stand-ins used under Vitest (typing, read-only, Ctrl/Cmd+S,
// revealLine) and, with the mode switched away from "test", the lazy Monaco path with
// MonacoEditor mocked at the module boundary.
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CodeEditor,
  type CodeEditorHandle,
  type CodeEditorProps,
  DiffView,
} from "./CodeEditor";

function Controlled({ onChange }: { onChange: (value: string) => void }) {
  const [value, setValue] = useState("a: 1\n");
  return (
    <CodeEditor
      label="Document"
      language="yaml"
      value={value}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
}

describe("CodeEditor (plain, under Vitest)", () => {
  it("is a labelled textarea that reports every edit", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<Controlled onChange={onChange} />);
    const area = screen.getByRole("textbox", { name: "Document" });
    expect(area).toHaveValue("a: 1\n");
    expect(area).toHaveAttribute("spellcheck", "false");
    await user.type(area, "b: 2");
    expect(area).toHaveValue("a: 1\nb: 2");
    expect(onChange).toHaveBeenLastCalledWith("a: 1\nb: 2");
  });

  it("tolerates edits without an onChange handler and honours readOnly", () => {
    render(<CodeEditor label="Ro" language="json" value="{}" readOnly />);
    const area = screen.getByRole("textbox", { name: "Ro" });
    expect(area).toHaveAttribute("readonly");
    // No handler: the change event must not throw.
    fireEvent.change(area, { target: { value: "{ }" } });
  });

  it("calls onSave on Ctrl+S and Cmd+S and prevents the browser save", () => {
    const onSave = vi.fn();
    render(<CodeEditor label="Doc" language="yaml" value="" onSave={onSave} />);
    const area = screen.getByRole("textbox", { name: "Doc" });
    const ctrl = fireEvent.keyDown(area, { key: "s", ctrlKey: true });
    expect(ctrl).toBe(false); // defaultPrevented
    fireEvent.keyDown(area, { key: "s", metaKey: true });
    expect(onSave).toHaveBeenCalledTimes(2);
    // Plain "s" and Ctrl+other keys do nothing.
    expect(fireEvent.keyDown(area, { key: "s" })).toBe(true);
    fireEvent.keyDown(area, { key: "a", ctrlKey: true });
    expect(onSave).toHaveBeenCalledTimes(2);
  });

  it("swallows Ctrl+S without an onSave handler", () => {
    render(<CodeEditor label="Doc" language="yaml" value="" />);
    const area = screen.getByRole("textbox", { name: "Doc" });
    expect(fireEvent.keyDown(area, { key: "s", ctrlKey: true })).toBe(false);
  });

  it("revealLine focuses the textarea and puts the caret at the line start", () => {
    const ref = createRef<CodeEditorHandle>();
    render(
      <CodeEditor
        ref={ref}
        label="Doc"
        language="yaml"
        value={"one\ntwo\nthree"}
      />,
    );
    const area = screen.getByRole<HTMLTextAreaElement>("textbox", {
      name: "Doc",
    });
    act(() => ref.current?.revealLine(3));
    expect(area).toHaveFocus();
    expect(area.selectionStart).toBe(8);
    expect(area.selectionEnd).toBe(8);
    act(() => ref.current?.revealLine(1));
    expect(area.selectionStart).toBe(0);
  });

  it("revealLine on a handle kept past unmount does nothing", () => {
    const ref = createRef<CodeEditorHandle>();
    const { unmount } = render(
      <CodeEditor ref={ref} label="Doc" language="yaml" value="one" />,
    );
    const handle = ref.current;
    unmount();
    expect(handle).not.toBeNull();
    expect(() => handle?.revealLine(1)).not.toThrow();
    expect(document.activeElement).toBe(document.body);
  });
});

describe("DiffView (plain, under Vitest)", () => {
  it("shows the before and after texts side by side", () => {
    render(<DiffView original="a: 1" modified="a: 2" language="yaml" />);
    expect(screen.getByLabelText("Before")).toHaveTextContent("a: 1");
    expect(screen.getByLabelText("After")).toHaveTextContent("a: 2");
  });
});

describe("fit on the plain stand-ins", () => {
  it("marks the textarea and the diff with data-fit", () => {
    const { container } = render(
      <>
        <CodeEditor label="Doc" language="yaml" value="" fit="fill" />
        <DiffView original="a" modified="b" language="yaml" fit="tall" />
      </>,
    );
    expect(screen.getByRole("textbox", { name: "Doc" })).toHaveAttribute(
      "data-fit",
      "fill",
    );
    expect(container.querySelector('[data-fit="tall"]')).not.toBeNull();
  });
});

describe("CodeEditor and DiffView outside test mode", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
    vi.doUnmock("./MonacoEditor");
  });

  async function loadMonacoPath() {
    vi.resetModules();
    vi.stubEnv("MODE", "production");
    vi.doMock("./MonacoEditor", () => ({
      default: (props: CodeEditorProps) => (
        <div data-testid="monaco" data-height={props.height}>
          {props.label}:{props.value}
        </div>
      ),
      MonacoDiff: (props: {
        original: string;
        modified: string;
        height?: string;
      }) => (
        <div data-testid="monaco-diff" data-height={props.height}>
          {props.original}|{props.modified}
        </div>
      ),
    }));
    return import("./CodeEditor");
  }

  it("renders the lazy Monaco editor instead of the textarea", async () => {
    const module = await loadMonacoPath();
    render(<module.CodeEditor label="Document" language="yaml" value="a: 1" />);
    expect(await screen.findByTestId("monaco")).toHaveTextContent(
      "Document:a: 1",
    );
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("renders the lazy Monaco diff", async () => {
    const module = await loadMonacoPath();
    render(<module.DiffView original="x" modified="y" language="json" />);
    expect(await screen.findByTestId("monaco-diff")).toHaveTextContent("x|y");
    expect(screen.queryByLabelText("Before")).not.toBeInTheDocument();
  });

  it("gives a fill frame its class and Monaco a 100% height", async () => {
    const module = await loadMonacoPath();
    render(
      <module.CodeEditor
        label="Document"
        language="yaml"
        value="a: 1"
        height="10rem"
        fit="fill"
      />,
    );
    const editor = await screen.findByTestId("monaco");
    expect(editor).toHaveAttribute("data-height", "100%");
    const frame = editor.closest("[data-fit]");
    expect(frame).toHaveAttribute("data-fit", "fill");
    expect(frame?.className).toMatch(/fill/);
  });

  it("passes the given height through without fit", async () => {
    const module = await loadMonacoPath();
    render(
      <module.CodeEditor
        label="Document"
        language="yaml"
        value="a: 1"
        height="10rem"
      />,
    );
    const editor = await screen.findByTestId("monaco");
    expect(editor).toHaveAttribute("data-height", "10rem");
    expect(editor.closest("[data-fit]")).toBeNull();
  });

  it("sizes a diff by fit", async () => {
    const module = await loadMonacoPath();
    render(
      <module.DiffView
        original="x"
        modified="y"
        language="json"
        fit="container"
      />,
    );
    const diff = await screen.findByTestId("monaco-diff");
    expect(diff).toHaveAttribute("data-height", "100%");
    expect(diff.closest("[data-fit]")?.className).toMatch(/container/);
  });
});
