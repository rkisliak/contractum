// The merged admin lists' search params: only known sources, a trimmed and capped search text,
// and a blank search treated as none.
import { describe, expect, it } from "vitest";
import { validateSourceSearch } from "./SourceList";

describe("validateSourceSearch", () => {
  it("keeps a known source and a trimmed, capped search text", () => {
    expect(validateSourceSearch({ source: "entra", q: "  ops  " })).toEqual({
      source: "entra",
      q: "ops",
    });
    expect(validateSourceSearch({ q: "x".repeat(250) }).q).toHaveLength(200);
  });

  it("drops an unknown source, a blank search and a non-text search", () => {
    expect(validateSourceSearch({ source: "ldap", q: "   " })).toEqual({});
    expect(validateSourceSearch({ q: 42 })).toEqual({});
  });
});
