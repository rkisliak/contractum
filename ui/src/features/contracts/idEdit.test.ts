import { describe, expect, it } from "vitest";
import { replaceTopLevelId } from "./idEdit";

const YAML_DOC = `apiVersion: v3.2.0
kind: DataContract
id: urn:acme:other # keep this comment
version: "1.0.0"
schema:
  - name: t
    properties:
      - name: id
        logicalType: string
`;

describe("replaceTopLevelId", () => {
  it("replaces only the top-level id of a YAML document", () => {
    const out = replaceTopLevelId(YAML_DOC, "yaml", "urn:acme:orders");
    expect(out).toBe(YAML_DOC.replace("urn:acme:other", "urn:acme:orders"));
    expect(out).toContain("# keep this comment");
    expect(out).toContain("      - name: id");
  });

  it("keeps working for a quoted YAML id and quotes an id that needs it", () => {
    const quoted = 'id: "urn:acme:other"\nkind: DataContract\n';
    expect(replaceTopLevelId(quoted, "yaml", "urn:acme:orders")).toBe(
      "id: urn:acme:orders\nkind: DataContract\n",
    );
    expect(replaceTopLevelId("id: abc\nkind: x\n", "yaml", "a: b #c")).toBe(
      'id: "a: b #c"\nkind: x\n',
    );
  });

  it("replaces only the top-level id of a JSON document", () => {
    const json = `{\n  "kind": "DataContract",\n  "id": "urn:acme:other",\n  "schema": [{ "name": "t", "id": "inner" }]\n}`;
    const out = replaceTopLevelId(json, "json", "urn:acme:orders");
    expect(out).toBe(json.replace("urn:acme:other", "urn:acme:orders"));
    expect(out).toContain('"id": "inner"');
  });

  it("gives null for a buffer it cannot edit safely", () => {
    expect(replaceTopLevelId("kind: x\n", "yaml", "a")).toBeNull();
    expect(replaceTopLevelId("id: [unclosed\n", "yaml", "a")).toBeNull();
    expect(replaceTopLevelId("id:\n  nested: 1\n", "yaml", "a")).toBeNull();
    expect(replaceTopLevelId('{"kind": 1}', "json", "a")).toBeNull();
    expect(replaceTopLevelId("{ broken", "json", "a")).toBeNull();
    expect(replaceTopLevelId("- a\n- b\n", "yaml", "a")).toBeNull();
  });
});

describe("replaceTopLevelId edge cases", () => {
  it("replaces a numeric JSON id with a string", () => {
    expect(replaceTopLevelId('{"id": 42}', "json", "urn:a")).toBe(
      '{"id": "urn:a"}',
    );
  });

  it("gives null for a JSON array or an id needing more than one YAML line", () => {
    expect(replaceTopLevelId('[{"id": "a"}]', "json", "urn:a")).toBeNull();
    expect(replaceTopLevelId("id: a\n", "yaml", "two\nlines")).toBeNull();
  });
});
