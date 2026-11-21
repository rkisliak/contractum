// MonacoEditor (the lazy chunk behind CodeEditor): findings become Monaco markers on the model,
// revealLine moves the cursor and focuses, Ctrl/Cmd+S calls the latest onSave and an emptied buffer
// reports "". MonacoDiff names both sides once mounted. The Monaco React wrapper and the Monaco
// setup are mocked at the module boundary (jsdom cannot run Monaco).
import { act, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CodeEditorHandle, EditorMarker } from "./CodeEditor";

type EditorProps = {
  value: string;
  height: string;
  theme: string;
  options: { readOnly: boolean; ariaLabel: string };
  onChange: (next: string | undefined) => void;
  onMount: (editor: unknown) => void;
};
type DiffProps = { onMount: (diff: unknown) => void; height: string };

type Hoisted = {
  editorProps: unknown[];
  diffProps: unknown[];
  setModelMarkers: ReturnType<typeof vi.fn>;
  mountWith: unknown;
};

const hoisted = vi.hoisted((): Hoisted => ({
  editorProps: [],
  diffProps: [],
  setModelMarkers: vi.fn(),
  mountWith: null,
}));

vi.mock("@monaco-editor/react", async () => {
  const { useEffect } = await import("react");
  function Editor(props: EditorProps) {
    hoisted.editorProps.push(props);
    const { onMount } = props;
    useEffect(() => {
      if (hoisted.mountWith) onMount(hoisted.mountWith);
    }, [onMount]);
    return <div data-testid="editor" data-height={props.height} />;
  }
  function DiffEditor(props: DiffProps) {
    hoisted.diffProps.push(props);
    return <div data-testid="diff" data-height={props.height} />;
  }
  return { default: Editor, DiffEditor };
});
vi.mock("./monacoSetup", () => ({
  editorTheme: (scheme: string) => `dc-${scheme}`,
  monaco: {
    MarkerSeverity: { Error: 8, Warning: 4, Info: 2 },
    KeyMod: { CtrlCmd: 2048 },
    KeyCode: { KeyS: 49 },
    editor: {
      EditorOption: { ariaLabel: 8 },
      setModelMarkers: hoisted.setModelMarkers,
    },
  },
}));

const { default: MonacoEditor, MonacoDiff } = await import("./MonacoEditor");

function lastEditorProps(): EditorProps {
  const props = hoisted.editorProps.at(-1);
  if (!props) throw new Error("Editor was not rendered");
  return props as EditorProps;
}

function fakeEditor(model: object | null = { id: "model" }) {
  const commands: (() => void)[] = [];
  const editor = {
    revealLineInCenter: vi.fn(),
    setPosition: vi.fn(),
    focus: vi.fn(),
    getModel: vi.fn(() => model),
    addCommand: vi.fn((_keys: number, run: () => void) => {
      commands.push(run);
    }),
  };
  return { editor, save: () => commands[0]?.() };
}

const marker: EditorMarker = {
  line: 2,
  column: 5,
  endLine: 2,
  endColumn: 5,
  severity: "warning",
  message: "Add a description.",
};

afterEach(() => {
  hoisted.editorProps.length = 0;
  hoisted.diffProps.length = 0;
  hoisted.mountWith = null;
  hoisted.setModelMarkers.mockClear();
});

describe("MonacoEditor", () => {
  it("passes the value, height, label, read-only flag and light theme to Monaco", () => {
    render(
      <MonacoEditor value="a: 1" language="yaml" label="Document" readOnly />,
    );
    expect(screen.getByTestId("editor")).toHaveAttribute(
      "data-height",
      "28rem",
    );
    const props = lastEditorProps();
    expect(props.value).toBe("a: 1");
    expect(props.theme).toBe("dc-light");
    expect(props.options).toMatchObject({
      readOnly: true,
      ariaLabel: "Document",
    });
  });

  it("reports edits, an emptied buffer as an empty string, and tolerates no handler", () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <MonacoEditor value="" language="json" label="Doc" onChange={onChange} />,
    );
    lastEditorProps().onChange("{}");
    lastEditorProps().onChange(undefined);
    expect(onChange.mock.calls).toEqual([["{}"], [""]]);
    rerender(<MonacoEditor value="" language="json" label="Doc" />);
    expect(() => {
      lastEditorProps().onChange("x");
    }).not.toThrow();
  });

  it("does nothing on revealLine before Monaco has mounted", () => {
    const ref = createRef<CodeEditorHandle>();
    render(<MonacoEditor ref={ref} value="" language="yaml" label="Doc" />);
    expect(() => {
      ref.current?.revealLine(3);
    }).not.toThrow();
    expect(hoisted.setModelMarkers).not.toHaveBeenCalled();
  });

  it("marks findings on the model and reveals a line once mounted", () => {
    const { editor } = fakeEditor();
    hoisted.mountWith = editor;
    const ref = createRef<CodeEditorHandle>();
    const { rerender } = render(
      <MonacoEditor ref={ref} value="a: 1" language="yaml" label="Doc" />,
    );
    // Markers are applied when the value or the findings change after mount.
    rerender(
      <MonacoEditor
        ref={ref}
        value="a: 1"
        language="yaml"
        label="Doc"
        markers={[marker, { ...marker, severity: "error", endColumn: 9 }]}
      />,
    );
    expect(hoisted.setModelMarkers).toHaveBeenLastCalledWith(
      { id: "model" },
      "dc-findings",
      [
        expect.objectContaining({
          startLineNumber: 2,
          startColumn: 5,
          // A zero-width range is widened to one character so it is visible.
          endColumn: 6,
          severity: 4,
          message: "Add a description.",
        }),
        expect.objectContaining({ endColumn: 9, severity: 8 }),
      ],
    );

    act(() => {
      ref.current?.revealLine(7);
    });
    expect(editor.revealLineInCenter).toHaveBeenCalledWith(7);
    expect(editor.setPosition).toHaveBeenCalledWith({
      lineNumber: 7,
      column: 1,
    });
    expect(editor.focus).toHaveBeenCalled();
    act(() => {
      ref.current?.revealLine(2, 4);
    });
    expect(editor.setPosition).toHaveBeenLastCalledWith({
      lineNumber: 2,
      column: 4,
    });
  });

  it("sets no markers while the editor has no model", () => {
    hoisted.mountWith = fakeEditor(null).editor;
    const { rerender } = render(
      <MonacoEditor value="a" language="yaml" label="Doc" />,
    );
    rerender(
      <MonacoEditor value="b" language="yaml" label="Doc" markers={[marker]} />,
    );
    expect(hoisted.setModelMarkers).not.toHaveBeenCalled();
  });

  it("binds Ctrl/Cmd+S to the latest onSave", () => {
    const fake = fakeEditor();
    hoisted.mountWith = fake.editor;
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(
      <MonacoEditor value="" language="yaml" label="Doc" onSave={first} />,
    );
    expect(fake.editor.addCommand).toHaveBeenCalledWith(
      2048 | 49,
      expect.any(Function),
    );
    rerender(
      <MonacoEditor value="" language="yaml" label="Doc" onSave={second} />,
    );
    fake.save();
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    rerender(<MonacoEditor value="" language="yaml" label="Doc" />);
    expect(() => {
      fake.save();
    }).not.toThrow();
  });
});

describe("MonacoDiff", () => {
  it("names each side of the diff with the given labels when it mounts", () => {
    render(
      <MonacoDiff
        original="a"
        modified="b"
        language="yaml"
        height="10rem"
        originalLabel="Server copy"
        modifiedLabel="Your edits"
      />,
    );
    expect(screen.getByTestId("diff")).toHaveAttribute("data-height", "10rem");
    const side = () => ({
      getOption: vi.fn(() => ""),
      updateOptions: vi.fn(),
      onDidChangeConfiguration: vi.fn(),
    });
    const original = side();
    const modified = side();
    const props = hoisted.diffProps.at(-1) as DiffProps;
    props.onMount({
      getOriginalEditor: () => original,
      getModifiedEditor: () => modified,
    });
    expect(original.updateOptions).toHaveBeenCalledWith({
      ariaLabel: "Server copy",
    });
    expect(modified.updateOptions).toHaveBeenCalledWith({
      ariaLabel: "Your edits",
    });
  });
});
