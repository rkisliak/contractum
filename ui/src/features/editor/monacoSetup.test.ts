// monacoSetup: the worker factory (JSON worker for "json", the editor worker otherwise), the CDN-free
// loader configuration and the editor themes built from the live token sheet. Monaco, its React
// loader and the bundled workers are mocked at the module boundary (jsdom cannot run them).
import { afterEach, describe, expect, it, vi } from "vitest";

type ThemeData = {
  base: string;
  inherit: boolean;
  rules: { token: string; foreground: string }[];
  colors: Record<string, string>;
};

const hoisted = vi.hoisted(() => ({
  defineTheme: vi.fn<(name: string, data: unknown) => void>(),
  config: vi.fn<(options: unknown) => void>(),
}));

vi.mock("monaco-editor", () => ({
  editor: { defineTheme: hoisted.defineTheme },
}));
vi.mock("@monaco-editor/react", () => ({
  loader: { config: hoisted.config },
}));
vi.mock("monaco-editor/editor/editor.worker?worker", () => ({
  default: class EditorWorker {
    kind = "editor";
  },
}));
vi.mock("monaco-editor/language/json/json.worker?worker", () => ({
  default: class JsonWorker {
    kind = "json";
  },
}));

const { editorTheme, monaco } = await import("./monacoSetup");
// The loader is configured once, at import; keep that call before any per-test mock reset.
const loaderConfigCalls = [...hoisted.config.mock.calls];

function definedTheme(): { name: string; data: ThemeData } {
  const call = hoisted.defineTheme.mock.lastCall;
  if (!call) throw new Error("defineTheme was not called");
  return { name: call[0], data: call[1] as ThemeData };
}

afterEach(() => {
  document.documentElement.removeAttribute("style");
  hoisted.defineTheme.mockClear();
});

describe("Monaco environment", () => {
  it("gives the JSON language its own worker and everything else the editor worker", () => {
    const environment = window.MonacoEnvironment;
    const json = environment?.getWorker?.("1", "json") as unknown as {
      kind: string;
    };
    const yaml = environment?.getWorker?.("2", "yaml") as unknown as {
      kind: string;
    };
    expect(json.kind).toBe("json");
    expect(yaml.kind).toBe("editor");
  });

  it("points the React loader at the bundled Monaco instead of a CDN", () => {
    expect(loaderConfigCalls).toEqual([[{ monaco }]]);
  });
});

describe("editorTheme", () => {
  it("builds the light theme from the token sheet with no syntax overrides", () => {
    document.documentElement.style.setProperty(
      "--color-neutral-primary-soft",
      "#ABCDEF",
    );
    document.documentElement.style.setProperty("--color-body", "#123456");
    expect(editorTheme("light")).toBe("dc-light");
    const { name, data } = definedTheme();
    expect(name).toBe("dc-light");
    expect(data.base).toBe("vs");
    expect(data.inherit).toBe(true);
    expect(data.rules).toEqual([]);
    expect(data.colors["editor.background"]).toBe("#ABCDEF");
    expect(data.colors["editorGutter.background"]).toBe("#ABCDEF");
    expect(data.colors["editorLineNumber.foreground"]).toBe("#123456");
    // An unset token falls back to the documented default.
    expect(data.colors["editorCursor.foreground"]).toBe("#193CB8");
    expect(data.colors).not.toHaveProperty("diffEditor.insertedLineBackground");
  });

  it("ignores a token that is not a six-digit hex colour", () => {
    document.documentElement.style.setProperty(
      "--color-heading",
      "rgb(1, 2, 3)",
    );
    editorTheme("light");
    expect(
      definedTheme().data.colors["editorLineNumber.activeForeground"],
    ).toBe("#111827");
  });

  it("builds the dark theme with lifted syntax colours and soft diff fills", () => {
    document.documentElement.style.setProperty("--color-body", "#A1B2C3");
    expect(editorTheme("dark")).toBe("dc-dark");
    const { data } = definedTheme();
    expect(data.base).toBe("vs-dark");
    expect(data.rules.map((rule) => rule.token)).toEqual([
      "annotation",
      "delimiter.html",
      "delimiter.xml",
      "metatag.html",
      "metatag.xml",
    ]);
    // Monaco takes rule colours without the leading "#".
    expect(data.rules.every((rule) => rule.foreground === "A1B2C3")).toBe(true);
    expect(data.colors).toMatchObject({
      "diffEditor.insertedLineBackground": "#002C22",
      "diffEditor.insertedTextBorder": "#10B981",
      "diffEditor.removedTextBackground": "#4D0218",
      "diffEditor.removedTextBorder": "#F87171",
    });
  });
});
