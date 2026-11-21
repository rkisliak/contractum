// Bundles Monaco through Vite (D-UI-09-10): the editor and JSON workers are emitted as same-origin
// files (CSP `worker-src 'self'`), and `loader.config({ monaco })` stops @monaco-editor/react from
// loading anything from a CDN (CSP `script-src 'self'`). Imported only by the lazy editor chunk.
import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/language/json/json.worker?worker";

declare global {
  interface Window {
    MonacoEnvironment?: monaco.Environment;
  }
}

window.MonacoEnvironment = {
  getWorker(_workerId: string, label: string) {
    return label === "json" ? new JsonWorker() : new EditorWorker();
  },
};

loader.config({ monaco });

// The editor themes read the enterprise token sheet at runtime, so no colour is hard-coded here.
// Tokens resolve for the active colour scheme, so each theme is (re)defined when it is selected.
function token(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return /^#[0-9a-fA-F]{6}$/.test(value) ? value : fallback;
}

export type EditorScheme = "light" | "dark";

/** Defines the theme for the current scheme from the live tokens and returns its name. */
export function editorTheme(scheme: EditorScheme): string {
  const name = `dc-${scheme}`;
  const surface = token("--color-neutral-primary-soft", "#FFFFFF");
  monaco.editor.defineTheme(name, {
    base: scheme === "dark" ? "vs-dark" : "vs",
    inherit: true,
    // vs-dark paints a few tokens #808080 (4.5:1 fails on dark surfaces): lift them to body.
    rules:
      scheme === "dark"
        ? [
            "annotation",
            "delimiter.html",
            "delimiter.xml",
            "metatag.html",
            "metatag.xml",
          ].map((name) => ({
            token: name,
            foreground: token("--color-body", "#9CA3AF").slice(1),
          }))
        : [],
    colors: {
      "editor.background": surface,
      "editorGutter.background": surface,
      "editorLineNumber.foreground": token("--color-body", "#4B5563"),
      "editorLineNumber.activeForeground": token("--color-heading", "#111827"),
      "editor.selectionBackground": token("--color-brand-soft", "#DBEAFE"),
      "editorCursor.foreground": token("--color-fg-brand", "#193CB8"),
      "editor.lineHighlightBorder": token(
        "--color-neutral-tertiary",
        "#F3F4F6",
      ),
      "editorWidget.background": token(
        "--color-neutral-primary-medium",
        "#FFFFFF",
      ),
      "editorWidget.border": token("--color-border-default", "#E5E7EB"),
      // Changed lines take the soft status fills and changed characters a border, not a stronger
      // fill: the vs-dark syntax colours drop under 4.5:1 on Monaco's default diff greens.
      ...(scheme === "dark"
        ? {
            "diffEditor.insertedLineBackground": token(
              "--color-success-soft",
              "#002C22",
            ),
            "diffEditor.insertedTextBackground": token(
              "--color-success-soft",
              "#002C22",
            ),
            "diffEditor.insertedTextBorder": token(
              "--color-fg-success-strong",
              "#10B981",
            ),
            "diffEditor.removedLineBackground": token(
              "--color-danger-soft",
              "#4D0218",
            ),
            "diffEditor.removedTextBackground": token(
              "--color-danger-soft",
              "#4D0218",
            ),
            "diffEditor.removedTextBorder": token(
              "--color-fg-danger-strong",
              "#F87171",
            ),
          }
        : {}),
    },
  });
  return name;
}

export { monaco };
