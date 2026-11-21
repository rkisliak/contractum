// The document editor used by the draft editor, version views, templates and diffs. Monaco is
// loaded lazily as its own chunk (bundled, no CDN; see monacoSetup.ts). Under Vitest (jsdom cannot
// run Monaco) a plain monospace textarea with the same props stands in, so screen tests can type.
//
// `fit` sizes the frame instead of a fixed `height`: "fill" reaches the bottom of the window (the
// last element of a page; useViewportFill), "tall" is a large fixed height for an editor with
// content after it, and "container" fills a flex-column parent such as the full-screen dialog.
import {
  lazy,
  Suspense,
  useImperativeHandle,
  useRef,
  type ReactNode,
  type Ref,
} from "react";
import { Skeleton } from "@/components/states";
import { cx } from "@/lib/cx";
import styles from "./CodeEditor.module.css";
import { useViewportFill } from "./useViewportFill";

export type EditorFit = "fill" | "tall" | "container";

export type EditorMarker = {
  line: number;
  column: number;
  endLine: number;
  endColumn: number;
  severity: "error" | "warning" | "info";
  message: string;
};

export type CodeEditorHandle = {
  revealLine: (line: number, column?: number) => void;
};

export type CodeEditorProps = {
  value: string;
  onChange?: (value: string) => void;
  language: "yaml" | "json";
  readOnly?: boolean;
  markers?: EditorMarker[];
  height?: string;
  /** Sizes the frame to the window or its container; overrides `height`. */
  fit?: EditorFit;
  /** Accessible name of the editor. */
  label: string;
  /** Ctrl/Cmd+S. */
  onSave?: () => void;
  ref?: Ref<CodeEditorHandle>;
};

export type DiffViewProps = {
  original: string;
  modified: string;
  language: "yaml" | "json";
  height?: string;
  /** Sizes the frame to the window or its container; overrides `height`. */
  fit?: EditorFit;
  /** Accessible names of the two sides (default "Before" and "After"). */
  originalLabel?: string;
  modifiedLabel?: string;
};

const MonacoEditor = lazy(() => import("./MonacoEditor"));
const MonacoDiff = lazy(() =>
  import("./MonacoEditor").then((module) => ({ default: module.MonacoDiff })),
);

const plain = import.meta.env.MODE === "test";

const fitClass: Record<EditorFit, string | undefined> = {
  fill: styles.fill,
  tall: styles.tall,
  container: styles.container,
};

/** The bordered frame around Monaco; a component of its own so its hook runs on every render. */
function EditorFrame({
  fit,
  children,
}: {
  fit: EditorFit | undefined;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  useViewportFill(ref, fit === "fill");
  return (
    <div
      ref={ref}
      className={cx(styles.frame, fit ? fitClass[fit] : undefined)}
      data-fit={fit}
    >
      {children}
    </div>
  );
}

function PlainEditor({
  value,
  onChange,
  readOnly,
  label,
  onSave,
  fit,
  ref,
}: CodeEditorProps) {
  const area = useRef<HTMLTextAreaElement | null>(null);
  useImperativeHandle(ref, () => ({
    revealLine(line: number) {
      const node = area.current;
      if (!node) return;
      const lines = node.value.split("\n");
      const offset = lines
        .slice(0, line - 1)
        .reduce((sum, text) => sum + text.length + 1, 0);
      node.focus();
      node.setSelectionRange(offset, offset);
    },
  }));
  return (
    <textarea
      ref={area}
      aria-label={label}
      className={styles.plain}
      data-fit={fit}
      value={value}
      readOnly={readOnly}
      spellCheck={false}
      onChange={(event) => onChange?.(event.target.value)}
      onKeyDown={(event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === "s") {
          event.preventDefault();
          onSave?.();
        }
      }}
    />
  );
}

export function CodeEditor(props: CodeEditorProps) {
  if (plain) return <PlainEditor {...props} />;
  const height = props.fit ? "100%" : props.height;
  return (
    <EditorFrame fit={props.fit}>
      <Suspense fallback={<Skeleton label="editor" rows={6} />}>
        <MonacoEditor {...props} height={height} />
      </Suspense>
    </EditorFrame>
  );
}

export function DiffView(props: DiffViewProps) {
  if (plain)
    return (
      <div className={styles.plainDiff} data-fit={props.fit}>
        <pre aria-label={props.originalLabel ?? "Before"}>{props.original}</pre>
        <pre aria-label={props.modifiedLabel ?? "After"}>{props.modified}</pre>
      </div>
    );
  const height = props.fit ? "100%" : props.height;
  return (
    <EditorFrame fit={props.fit}>
      <Suspense fallback={<Skeleton label="comparison" rows={6} />}>
        <MonacoDiff {...props} height={height} />
      </Suspense>
    </EditorFrame>
  );
}
