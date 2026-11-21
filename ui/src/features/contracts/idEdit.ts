// A minimal, safe edit of the top-level `id` of an ODCS document in the editor buffer (quick
// 261119-wk8): only the value of the root `id` key changes, every other byte stays. Used by "Put
// the contract's id back" after a 422 odcs-id-mismatch. A buffer that does not parse, has no root
// `id` or whose `id` is not a plain value gives null: the button is then not offered.
import { parseTree, type Node, type ParseError } from "jsonc-parser";
import { isMap, isScalar, parseDocument, stringify } from "yaml";
import type { DocumentLanguage } from "@/features/editor/pointerMap";

function replaceRange(
  text: string,
  start: number,
  end: number,
  value: string,
): string {
  return text.slice(0, start) + value + text.slice(end);
}

function jsonReplace(text: string, id: string): string | null {
  const errors: ParseError[] = [];
  const root = parseTree(text, errors);
  if (!root || errors.length > 0 || root.type !== "object") return null;
  const property = root.children?.find(
    (child) => child.children?.[0]?.value === "id",
  );
  const value: Node | undefined = property?.children?.[1];
  if (!value || (value.type !== "string" && value.type !== "number"))
    return null;
  return replaceRange(
    text,
    value.offset,
    value.offset + value.length,
    JSON.stringify(id),
  );
}

function yamlReplace(text: string, id: string): string | null {
  const doc = parseDocument(text, { uniqueKeys: false });
  if (doc.errors.length > 0 || !isMap(doc.contents)) return null;
  const pair = doc.contents.items.find(
    (item) => isScalar(item.key) && String(item.key.value) === "id",
  );
  const value = pair?.value;
  if (!isScalar(value)) return null;
  // `yaml` writes a plain scalar when it is safe and a quoted one otherwise. A value range ends
  // at the scalar's last character (a trailing comment or newline is outside it).
  const written = stringify(id, { lineWidth: 0 }).trimEnd();
  if (written.includes("\n")) return null;
  const [start, valueEnd] = value.range;
  // `range[1]` is the end of the scalar's value; `range[2]` includes trailing whitespace. Keep
  // the trailing part so comments and line breaks are untouched.
  return replaceRange(text, start, valueEnd, written);
}

/** `text` with the top-level `id` set to `id`, or null when that edit cannot be made safely. */
export function replaceTopLevelId(
  text: string,
  language: DocumentLanguage,
  id: string,
): string | null {
  try {
    return language === "json" ? jsonReplace(text, id) : yamlReplace(text, id);
  } catch {
    return null;
  }
}
