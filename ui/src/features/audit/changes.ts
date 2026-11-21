// The RFC 6902 change renderer (D-UI-13-07): each patch operation becomes a readable row with the
// old value (looked up in `before` by its RFC 6901 pointer) and the new value.

export type ChangeRow = {
  op: string;
  path: string;
  /** `move`/`copy` source pointer. */
  from?: string;
  oldValue?: unknown;
  newValue?: unknown;
  hasOld: boolean;
  hasNew: boolean;
};

const MISSING = Symbol("missing");

/** Resolve an RFC 6901 JSON Pointer; MISSING when any step is absent. */
export function resolvePointer(document: unknown, pointer: string): unknown {
  if (pointer === "") return document;
  if (!pointer.startsWith("/")) return MISSING;
  let current: unknown = document;
  for (const raw of pointer.slice(1).split("/")) {
    const token = raw.replace(/~1/g, "/").replace(/~0/g, "~");
    if (Array.isArray(current)) {
      const index = Number(token);
      if (!Number.isInteger(index) || index < 0 || index >= current.length)
        return MISSING;
      current = current[index];
    } else if (current !== null && typeof current === "object") {
      if (!Object.prototype.hasOwnProperty.call(current, token)) return MISSING;
      current = (current as Record<string, unknown>)[token];
    } else {
      return MISSING;
    }
  }
  return current;
}

export function describeChanges(
  changes: Record<string, unknown>[],
  before: unknown,
): ChangeRow[] {
  return changes.map((change) => {
    const op = typeof change.op === "string" ? change.op : "unknown";
    const path = typeof change.path === "string" ? change.path : "";
    const from = typeof change.from === "string" ? change.from : undefined;
    const old =
      op === "add" || op === "copy" || op === "move"
        ? MISSING
        : resolvePointer(before, path);
    const hasNew =
      op === "add" || op === "replace" || op === "test"
        ? "value" in change
        : op === "move" || op === "copy";
    const newValue =
      op === "move" || op === "copy"
        ? resolvePointer(before, from ?? "")
        : change.value;
    return {
      op,
      path,
      ...(from === undefined ? {} : { from }),
      hasOld: old !== MISSING,
      ...(old === MISSING ? {} : { oldValue: old }),
      hasNew: hasNew && newValue !== MISSING,
      ...(hasNew && newValue !== MISSING ? { newValue } : {}),
    };
  });
}

/** A compact display string for a JSON value. */
export function formatValue(value: unknown): string {
  if (value === undefined) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

export const opLabels: Record<string, string> = {
  add: "Added",
  remove: "Removed",
  replace: "Changed",
  move: "Moved",
  copy: "Copied",
  test: "Tested",
};
