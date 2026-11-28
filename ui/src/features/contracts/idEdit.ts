// A minimal, safe edit of one top-level value of an ODCS document in the editor buffer (quick
// 261119-wk8): only the value of the root key changes, every other byte stays. Used by "Put the
// contract's id back" after a 422 odcs-id-mismatch and by the publish dialog's "Change status to
// active". A buffer that does not parse, has no such root key or whose value is not a plain value
// gives null: the button is then not offered.
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

type JsonValueType = Node["type"];

function jsonReplace(
  text: string,
  key: string,
  replacement: string,
  types: readonly JsonValueType[],
): string | null {
  const errors: ParseError[] = [];
  const root = parseTree(text, errors);
  if (!root || errors.length > 0 || root.type !== "object") return null;
  const property = root.children?.find(
    (child) => child.children?.[0]?.value === key,
  );
  const value: Node | undefined = property?.children?.[1];
  if (!value || !types.includes(value.type)) return null;
  return replaceRange(
    text,
    value.offset,
    value.offset + value.length,
    JSON.stringify(replacement),
  );
}

function yamlReplace(
  text: string,
  key: string,
  replacement: string,
): string | null {
  const doc = parseDocument(text, { uniqueKeys: false });
  if (doc.errors.length > 0 || !isMap(doc.contents)) return null;
  const pair = doc.contents.items.find(
    (item) => isScalar(item.key) && String(item.key.value) === key,
  );
  const value = pair?.value;
  if (!isScalar(value)) return null;
  // `yaml` writes a plain scalar when it is safe and a quoted one otherwise. A value range ends
  // at the scalar's last character (a trailing comment or newline is outside it).
  const written = stringify(replacement, { lineWidth: 0 }).trimEnd();
  if (written.includes("\n")) return null;
  const [start, valueEnd] = value.range;
  // An empty value (`key:` with nothing after it) has no characters to replace; writing there
  // would glue the value to the colon, so that edit is not offered.
  if (valueEnd <= start) return null;
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
    return language === "json"
      ? jsonReplace(text, "id", id, ["string", "number"])
      : yamlReplace(text, "id", id);
  } catch {
    return null;
  }
}

/**
 * `text` with the value of the top-level ODCS `status` set to `status`, or null when that edit
 * cannot be made safely (no root `status` key, a nested value, a buffer that does not parse). A
 * missing `status` is never inserted: only an existing value is rewritten.
 */
export function replaceTopLevelStatus(
  text: string,
  language: DocumentLanguage,
  status: string,
): string | null {
  try {
    return language === "json"
      ? jsonReplace(text, "status", status, [
          "string",
          "number",
          "boolean",
          "null",
        ])
      : yamlReplace(text, "status", status);
  } catch {
    return null;
  }
}
