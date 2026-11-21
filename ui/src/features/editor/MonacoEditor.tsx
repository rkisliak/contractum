// The Monaco implementation behind CodeEditor (lazy chunk). Markers come from findings mapped by
// pointerMap; `revealLine` moves the cursor to a finding and focuses the editor.
import Editor, { DiffEditor, type OnMount } from "@monaco-editor/react";
import { useEffect, useImperativeHandle, useMemo, useRef } from "react";
import type { CodeEditorProps, DiffViewProps } from "./CodeEditor";
import { editorTheme, monaco } from "./monacoSetup";
import { useColorScheme } from "./useColorScheme";

const severityOf = {
  error: monaco.MarkerSeverity.Error,
  warning: monaco.MarkerSeverity.Warning,
  info: monaco.MarkerSeverity.Info,
} as const;

export default function MonacoEditor({
  value,
  onChange,
  language,
  readOnly = false,
  markers = [],
  height = "28rem",
  label,
  onSave,
  ref,
}: CodeEditorProps) {
  const scheme = useColorScheme();
  const theme = useMemo(() => editorTheme(scheme), [scheme]);
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const saveRef = useRef(onSave);
  useEffect(() => {
    saveRef.current = onSave;
  }, [onSave]);

  useImperativeHandle(ref, () => ({
    revealLine(line: number, column = 1) {
      const editor = editorRef.current;
      if (!editor) return;
      editor.revealLineInCenter(line);
      editor.setPosition({ lineNumber: line, column });
      editor.focus();
    },
  }));

  useEffect(() => {
    const model = editorRef.current?.getModel();
    if (!model) return;
    monaco.editor.setModelMarkers(
      model,
      "dc-findings",
      markers.map((marker) => ({
        startLineNumber: marker.line,
        startColumn: marker.column,
        endLineNumber: marker.endLine,
        endColumn: Math.max(marker.endColumn, marker.column + 1),
        severity: severityOf[marker.severity],
        message: marker.message,
      })),
    );
  }, [markers, value]);

  const onMount: OnMount = (editor) => {
    editorRef.current = editor;
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      saveRef.current?.();
    });
  };

  return (
    <Editor
      height={height}
      language={language}
      value={value}
      theme={theme}
      onChange={(next) => onChange?.(next ?? "")}
      onMount={onMount}
      options={{
        readOnly,
        ariaLabel: label,
        minimap: { enabled: false },
        fontSize: 13,
        scrollBeyondLastLine: false,
        tabSize: 2,
        automaticLayout: true,
        wordWrap: "on",
      }}
    />
  );
}

type DiffSide = Pick<
  monaco.editor.ICodeEditor,
  "getOption" | "updateOptions" | "onDidChangeConfiguration"
>;

/**
 * Keeps an accessible name on one side of a diff editor. Monaco's diff editor resets each side's
 * `ariaLabel` to "" on every later diff-level options update that does not repeat
 * `originalAriaLabel`/`modifiedAriaLabel` (for example its own layout changes), which left both
 * inputs unnamed (axe aria-input-field-name on /contracts/$contractId/diff). The listener is
 * disposed with the editor.
 */
export function nameDiffSide(editor: DiffSide, label: string): void {
  const apply = () => {
    if (!editor.getOption(monaco.editor.EditorOption.ariaLabel))
      editor.updateOptions({ ariaLabel: label });
  };
  apply();
  editor.onDidChangeConfiguration(apply);
}

export function MonacoDiff({
  original,
  modified,
  language,
  height = "32rem",
  originalLabel = "Before",
  modifiedLabel = "After",
}: DiffViewProps) {
  const scheme = useColorScheme();
  const theme = useMemo(() => editorTheme(scheme), [scheme]);
  return (
    <DiffEditor
      height={height}
      language={language}
      original={original}
      modified={modified}
      theme={theme}
      onMount={(diff) => {
        nameDiffSide(diff.getOriginalEditor(), originalLabel);
        nameDiffSide(diff.getModifiedEditor(), modifiedLabel);
      }}
      options={{
        // Each side's input needs an accessible name (axe aria-input-field-name): set at
        // construction here, and kept by nameDiffSide afterwards.
        originalAriaLabel: originalLabel,
        modifiedAriaLabel: modifiedLabel,
        readOnly: true,
        originalEditable: false,
        minimap: { enabled: false },
        fontSize: 13,
        automaticLayout: true,
        renderSideBySide: true,
      }}
    />
  );
}
