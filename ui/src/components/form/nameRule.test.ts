import { describe, expect, it } from "vitest";
import { NAME_RULE_MESSAGE, nameRuleError } from "./nameRule";

// The same samples as shared/dc-core/tests/unit/test_names.py.
const ACCEPTED = [
  "Ledger team",
  "Équipe données",
  "数据团队",
  "فريق البيانات",
  "צוות",
  "हिन्दी",
  "Team 42 (EU) & Co.",
  "a-b_c.d,e/f:g#h+i@j'k",
  "  Ledger  ",
];

const REFUSED = [
  "😀 team",
  "Team\u202eevil",
  "a\u2066b",
  "zero\u200bwidth",
  "\ufeffbom",
  "tab\there",
  "line\nbreak",
  "a b",
  "a b",
  "price $5",
  "a=b",
  "a^b",
  "©",
  "\ue000",
  "a\u0378b",
  "1\ufe0f\u20e3",
  "a\u00a0b",
  "O’Brien",
  "a\u200db",
  "a\u200cb",
  "1\u20e3",
];

describe("nameRuleError", () => {
  it("states the exact message", () => {
    expect(NAME_RULE_MESSAGE).toBe(
      "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.",
    );
  });

  it.each(ACCEPTED)("accepts %j", (value) => {
    expect(nameRuleError(value)).toBeUndefined();
  });

  it.each(REFUSED)("refuses %j", (value) => {
    expect(nameRuleError(value)).toBe(NAME_RULE_MESSAGE);
  });

  it("leaves blank to the required-field message", () => {
    expect(nameRuleError("")).toBeUndefined();
    expect(nameRuleError("   ")).toBeUndefined();
  });
});
