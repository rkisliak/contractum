// Direct unit tests of the RFC 6902 change renderer: pointer resolution, every op and the
// malformed-operation fallbacks, and the value formatter.
import { describe, expect, it } from "vitest";
import {
  describeChanges,
  formatValue,
  opLabels,
  resolvePointer,
} from "./changes";

describe("resolvePointer", () => {
  const doc = { a: { b: [10, { c: "deep" }] }, n: null, "": "empty key" };

  it("returns the whole document for the empty pointer", () => {
    expect(resolvePointer(doc, "")).toBe(doc);
  });

  it("walks objects and arrays", () => {
    expect(resolvePointer(doc, "/a/b/0")).toBe(10);
    expect(resolvePointer(doc, "/a/b/1/c")).toBe("deep");
    expect(resolvePointer(doc, "/")).toBe("empty key");
  });

  it("resolves a present null", () => {
    expect(resolvePointer(doc, "/n")).toBeNull();
  });

  it("is missing for a pointer without a leading slash", () => {
    expect(describeOld(doc, "a")).toBe(false);
  });

  it("is missing for bad array indexes", () => {
    expect(describeOld(doc, "/a/b/2")).toBe(false);
    expect(describeOld(doc, "/a/b/-1")).toBe(false);
    expect(describeOld(doc, "/a/b/x")).toBe(false);
    expect(describeOld(doc, "/a/b/1.5")).toBe(false);
  });

  it("is missing for an absent key, an inherited key or stepping into a scalar", () => {
    expect(describeOld(doc, "/zzz")).toBe(false);
    expect(describeOld(doc, "/toString")).toBe(false);
    expect(describeOld(doc, "/a/b/0/x")).toBe(false);
    expect(describeOld(doc, "/n/x")).toBe(false);
  });
});

/** hasOld of a `remove` at the pointer: false exactly when resolvePointer is MISSING. */
function describeOld(before: unknown, path: string): boolean {
  return describeChanges([{ op: "remove", path }], before)[0]?.hasOld ?? false;
}

describe("describeChanges", () => {
  const before = { name: "old", list: ["x", "y"], src: { k: 1 } };

  it("renders replace and test with old and new values", () => {
    expect(
      describeChanges(
        [
          { op: "replace", path: "/name", value: "new" },
          { op: "test", path: "/name", value: "old" },
        ],
        before,
      ),
    ).toEqual([
      {
        op: "replace",
        path: "/name",
        hasOld: true,
        oldValue: "old",
        hasNew: true,
        newValue: "new",
      },
      {
        op: "test",
        path: "/name",
        hasOld: true,
        oldValue: "old",
        hasNew: true,
        newValue: "old",
      },
    ]);
  });

  it("has no new value when add or replace omit `value`", () => {
    const [add, replace] = describeChanges(
      [
        { op: "add", path: "/x" },
        { op: "replace", path: "/name" },
      ],
      before,
    );
    expect(add).toEqual({
      op: "add",
      path: "/x",
      hasOld: false,
      hasNew: false,
    });
    expect(replace).toMatchObject({ hasOld: true, hasNew: false });
    expect(replace).not.toHaveProperty("newValue");
  });

  it("takes the new value of move and copy from the source pointer", () => {
    expect(
      describeChanges(
        [
          { op: "move", from: "/src", path: "/dst" },
          { op: "copy", from: "/list/1", path: "/list/2" },
        ],
        before,
      ),
    ).toEqual([
      {
        op: "move",
        path: "/dst",
        from: "/src",
        hasOld: false,
        hasNew: true,
        newValue: { k: 1 },
      },
      {
        op: "copy",
        path: "/list/2",
        from: "/list/1",
        hasOld: false,
        hasNew: true,
        newValue: "y",
      },
    ]);
  });

  it("has no new value when the move source is missing or absent", () => {
    const [missingSource, noFrom] = describeChanges(
      [
        { op: "move", from: "/nope", path: "/dst" },
        { op: "copy", path: "/dst" },
      ],
      { only: 1 },
    );
    expect(missingSource).toMatchObject({ hasNew: false, from: "/nope" });
    expect(missingSource).not.toHaveProperty("newValue");
    // Without `from`, the source is the empty pointer: the whole `before` document.
    expect(noFrom).toMatchObject({ hasNew: true, newValue: { only: 1 } });
    expect(noFrom).not.toHaveProperty("from");
  });

  it("falls back for a malformed operation", () => {
    expect(
      describeChanges([{ op: 7, path: 9, from: 3, value: "v" }], "root"),
    ).toEqual([
      {
        op: "unknown",
        path: "",
        hasOld: true,
        oldValue: "root",
        hasNew: false,
      },
    ]);
  });

  it("has no old value for a remove whose target is absent", () => {
    expect(describeChanges([{ op: "remove", path: "/gone" }], null)).toEqual([
      { op: "remove", path: "/gone", hasOld: false, hasNew: false },
    ]);
  });
});

describe("formatValue", () => {
  it("formats each value kind", () => {
    expect(formatValue(undefined)).toBe("");
    expect(formatValue("text")).toBe("text");
    expect(formatValue(3)).toBe("3");
    expect(formatValue(null)).toBe("null");
    expect(formatValue({ a: [1] })).toBe('{"a":[1]}');
  });
});

describe("opLabels", () => {
  it("labels every RFC 6902 operation", () => {
    expect(opLabels).toEqual({
      add: "Added",
      remove: "Removed",
      replace: "Changed",
      move: "Moved",
      copy: "Copied",
      test: "Tested",
    });
  });
});
