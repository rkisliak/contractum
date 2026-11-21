import { describe, expect, it } from "vitest";
import { languageOf, locatePointer, parsePointer } from "./pointerMap";

const yaml = `apiVersion: v3.1.0
kind: DataContract
id: urn:x
version: 1.0.0
"a/b": slash
schema:
  - name: orders
    properties:
      - name: id
      - name: amount
`;

const json = `{
  "apiVersion": "v3.1.0",
  "schema": [
    { "name": "orders", "properties": [{ "name": "id" }, { "name": "amount" }] }
  ],
  "a~b": 1
}`;

describe("parsePointer", () => {
  it("splits and unescapes RFC 6901 pointers", () => {
    expect(parsePointer("")).toEqual([]);
    expect(parsePointer("/a~1b/c~0d/0")).toEqual(["a/b", "c~d", "0"]);
    expect(parsePointer("no-slash")).toBeNull();
  });
});

describe("locatePointer in YAML", () => {
  it("finds top-level keys, array members and escaped keys", () => {
    expect(locatePointer(yaml, "/version", "yaml")?.line).toBe(4);
    expect(locatePointer(yaml, "/a~1b", "yaml")?.line).toBe(5);
    expect(locatePointer(yaml, "/schema/0/properties/1", "yaml")).toMatchObject(
      {
        line: 10,
        atParent: false,
      },
    );
  });

  it("falls back to the nearest ancestor for a missing member", () => {
    expect(locatePointer(yaml, "/schema/0/description", "yaml")).toMatchObject({
      line: 7,
      atParent: true,
    });
  });

  it("maps the empty pointer to the document start and a broken buffer to null", () => {
    expect(locatePointer(yaml, "", "yaml")?.line).toBe(1);
    expect(locatePointer("a: [1, 2", "/a", "yaml")).toBeNull();
  });
});

describe("locatePointer in JSON", () => {
  it("finds nested array members and escaped keys", () => {
    expect(locatePointer(json, "/apiVersion", "json")?.line).toBe(2);
    expect(locatePointer(json, "/schema/0/properties/1", "json")?.line).toBe(4);
    expect(locatePointer(json, "/a~0b", "json")?.line).toBe(6);
  });

  it("falls back to the parent and rejects invalid JSON", () => {
    expect(locatePointer(json, "/schema/3", "json")).toMatchObject({
      line: 3,
      atParent: true,
    });
    expect(locatePointer("{", "/a", "json")).toBeNull();
  });
});

describe("languageOf", () => {
  it("prefers the media type, then sniffs the text", () => {
    expect(languageOf("application/json")).toBe("json");
    expect(languageOf("application/yaml")).toBe("yaml");
    expect(languageOf(null, ' {"a":1}')).toBe("json");
    expect(languageOf(undefined, "a: 1")).toBe("yaml");
  });
});

describe("locatePointer edge cases", () => {
  it("marks an explicit YAML key that has no value", () => {
    expect(locatePointer("a: 1\n? b\n", "/b", "yaml")).toEqual({
      line: 2,
      column: 3,
      endLine: 2,
      endColumn: 4,
      atParent: false,
    });
  });

  it("matches a complex YAML key by its text and marks its value", () => {
    expect(
      locatePointer("? [x, y]\n: 1\n", '/["x","y"]', "yaml"),
    ).toMatchObject({ line: 2, column: 3, atParent: false });
    // The same key without a value has no range of its own: the document is marked.
    expect(locatePointer("? [x, y]\n", '/["x","y"]', "yaml")).toMatchObject({
      line: 1,
      column: 1,
      atParent: true,
    });
  });

  it("falls back to the parent below a scalar or past a YAML list", () => {
    expect(locatePointer(yaml, "/version/major", "yaml")).toMatchObject({
      line: 4,
      atParent: true,
    });
    expect(locatePointer(yaml, "/schema/first", "yaml")).toMatchObject({
      line: 6,
      atParent: true,
    });
    expect(locatePointer(yaml, "/schema/9", "yaml")).toMatchObject({
      line: 6,
      atParent: true,
    });
  });

  it("maps pointers into an empty or scalar YAML document to its start", () => {
    expect(locatePointer("", "", "yaml")).toEqual({
      line: 1,
      column: 1,
      endLine: 1,
      endColumn: 1,
      atParent: false,
    });
    expect(locatePointer("", "/a", "yaml")).toMatchObject({
      line: 1,
      atParent: true,
    });
    expect(locatePointer("x\n", "", "yaml")).toMatchObject({
      line: 1,
      endColumn: 2,
      atParent: false,
    });
  });

  it("falls back to the parent below a JSON scalar or for a non-numeric index", () => {
    expect(locatePointer(json, "/apiVersion/major", "json")).toMatchObject({
      line: 2,
      atParent: true,
    });
    expect(locatePointer(json, "/schema/first", "json")).toMatchObject({
      line: 3,
      atParent: true,
    });
  });

  it("gives null for a pointer that does not start with a slash", () => {
    expect(locatePointer(yaml, "version", "yaml")).toBeNull();
    expect(locatePointer(json, "apiVersion", "json")).toBeNull();
  });
});
