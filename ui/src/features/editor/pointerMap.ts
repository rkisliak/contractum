// Maps an RFC 6901 JSON Pointer to a text range in a YAML or JSON buffer (D-UI-09-15), so
// findings from validation, the publish gate and the diff can be marked in the editor. A pointer
// to a missing member falls back to its nearest existing ancestor (`atParent`). A buffer that does
// not parse gives null: the finding is still listed, marked "location unavailable".
import { parseTree, type Node, type ParseError } from "jsonc-parser";
import { isMap, isScalar, isSeq, parseDocument } from "yaml";

export type TextRange = {
  line: number;
  column: number;
  endLine: number;
  endColumn: number;
  /** True when the pointer's own member is missing and the range is its nearest ancestor. */
  atParent: boolean;
};

export type DocumentLanguage = "yaml" | "json";

export function parsePointer(pointer: string): string[] | null {
  if (pointer === "") return [];
  if (!pointer.startsWith("/")) return null;
  return pointer
    .slice(1)
    .split("/")
    .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
}

export function languageOf(
  mediaType: string | null | undefined,
  text?: string,
): DocumentLanguage {
  if (mediaType?.includes("json")) return "json";
  if (mediaType?.includes("yaml")) return "yaml";
  return text?.trimStart().startsWith("{") ? "json" : "yaml";
}

function positionOf(
  offsets: number[],
  offset: number,
): { line: number; column: number } {
  // offsets[i] is the start offset of line i + 1.
  let low = 0;
  let high = offsets.length - 1;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if ((offsets[mid] ?? 0) <= offset) low = mid;
    else high = mid - 1;
  }
  return { line: low + 1, column: offset - (offsets[low] ?? 0) + 1 };
}

function lineStarts(text: string): number[] {
  const starts = [0];
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === "\n") starts.push(index + 1);
  }
  return starts;
}

function toRange(
  text: string,
  start: number,
  end: number,
  atParent: boolean,
): TextRange {
  const offsets = lineStarts(text);
  const from = positionOf(offsets, start);
  const to = positionOf(offsets, Math.max(start, end));
  return {
    line: from.line,
    column: from.column,
    endLine: to.line,
    endColumn: to.column,
    atParent,
  };
}

function locateYaml(text: string, segments: string[]): TextRange | null {
  const doc = parseDocument(text, { uniqueKeys: false });
  if (doc.errors.length > 0) return null;
  let node: unknown = doc.contents;
  let range: [number, number] | null = null;
  const rootRange = (node as { range?: [number, number, number] } | null)
    ?.range;
  if (rootRange) range = [rootRange[0], rootRange[1]];
  let atParent = false;
  for (const segment of segments) {
    if (isMap(node)) {
      const pair = node.items.find(
        (item) =>
          (isScalar(item.key) ? String(item.key.value) : String(item.key)) ===
          segment,
      );
      if (!pair) {
        atParent = true;
        break;
      }
      const keyRange = isScalar(pair.key) ? pair.key.range : undefined;
      // An explicit key with no value (`? key`) has a null value: fall back to the key's range.
      const valueRange = (
        pair.value as { range?: [number, number, number] } | null
      )?.range;
      const start = keyRange?.[0] ?? valueRange?.[0];
      const end = valueRange?.[1] ?? keyRange?.[1];
      if (start === undefined || end === undefined) {
        atParent = true;
        break;
      }
      range = [start, end];
      node = pair.value;
    } else if (isSeq(node)) {
      const index = Number(segment);
      const item: unknown = Number.isInteger(index)
        ? node.items[index]
        : undefined;
      const itemRange = (item as { range?: [number, number, number] } | null)
        ?.range;
      if (!itemRange) {
        atParent = true;
        break;
      }
      range = [itemRange[0], itemRange[1]];
      node = item;
    } else {
      atParent = true;
      break;
    }
  }
  if (range === null) return toRange(text, 0, 0, atParent);
  return toRange(text, range[0], range[1], atParent);
}

function locateJson(text: string, segments: string[]): TextRange | null {
  const errors: ParseError[] = [];
  const root = parseTree(text, errors);
  if (!root || errors.length > 0) return null;
  let node: Node = root;
  let holder: Node = root;
  let atParent = false;
  for (const segment of segments) {
    let next: Node | undefined;
    let nextHolder: Node | undefined;
    if (node.type === "object") {
      const property = node.children?.find(
        (child) => child.children?.[0]?.value === segment,
      );
      next = property?.children?.[1];
      nextHolder = property;
    } else if (node.type === "array") {
      const index = Number(segment);
      next = Number.isInteger(index) ? node.children?.[index] : undefined;
      nextHolder = next;
    }
    if (!next || !nextHolder) {
      atParent = true;
      break;
    }
    node = next;
    holder = nextHolder;
  }
  return toRange(text, holder.offset, holder.offset + holder.length, atParent);
}

/** The text range of `pointer` in `text`, or null when the buffer does not parse. */
export function locatePointer(
  text: string,
  pointer: string,
  language: DocumentLanguage,
): TextRange | null {
  const segments = parsePointer(pointer);
  if (segments === null) return null;
  try {
    return language === "json"
      ? locateJson(text, segments)
      : locateYaml(text, segments);
  } catch {
    return null;
  }
}
