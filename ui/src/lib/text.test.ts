import { describe, expect, it } from "vitest";
import {
  collapseTrailingWhitespace,
  compareText,
  firstSemver,
  toSlug,
  trimChar,
  trimCharEnd,
} from "./text";

// A hostile 200,001-character input: the old trim and trailing-whitespace regexes
// backtracked quadratically on it. Every helper here must answer within the (default 5 s) test timeout.
const LONG = 100_000;
const hyphens = "-".repeat(LONG);

describe("trimChar and trimCharEnd", () => {
  it("trims the character from both ends, or from the end only", () => {
    expect(trimChar("--a-b--", "-")).toBe("a-b");
    expect(trimChar("----", "-")).toBe("");
    expect(trimChar("", "-")).toBe("");
    expect(trimChar("a", "-")).toBe("a");
    expect(trimCharEnd("/api/v1///", "/")).toBe("/api/v1");
    expect(trimCharEnd("///", "/")).toBe("");
    expect(trimCharEnd("--a", "-")).toBe("--a");
  });

  it("stays linear on 100,000-character adversarial input", () => {
    const input = `${hyphens}x${hyphens}`;
    expect(trimChar(input, "-")).toBe("x");
    expect(trimCharEnd(input, "-")).toBe(`${hyphens}x`);
    expect(trimChar(hyphens, "-")).toBe("");
    expect(trimCharEnd("/".repeat(LONG), "/")).toBe("");
  });
});

describe("toSlug", () => {
  it("lowercases and collapses every run of other characters to one hyphen", () => {
    expect(toSlug("Ledger Team!")).toBe("ledger-team");
    expect(toSlug("  --Ää--  ")).toBe("");
    expect(toSlug("a__b  c")).toBe("a-b-c");
    expect(toSlug("")).toBe("");
  });

  it("cuts to maxLength and drops a trailing hyphen", () => {
    const long = `${"a".repeat(62)} b`; // the hyphen would be character 63
    expect(toSlug("a".repeat(100), 63)).toBe("a".repeat(63));
    expect(toSlug(long, 63)).toBe("a".repeat(62));
    expect(toSlug(long)).toBe(`${"a".repeat(62)}-b`);
  });

  it("stays linear on 100,000-character adversarial input", () => {
    expect(toSlug(`${hyphens}x${hyphens}`, 63)).toBe("x");
    expect(toSlug(" ".repeat(LONG) + "a", 63)).toBe("a");
  });
});

describe("firstSemver", () => {
  it("returns the leftmost x.y.z exactly like the old first-match regex", () => {
    expect(firstSemver("need at least 2.0.0")).toBe("2.0.0");
    expect(firstSemver("v1.2.3.4")).toBe("1.2.3");
    expect(firstSemver("1..2.3.4")).toBe("2.3.4");
    expect(firstSemver("10.20.30 and 1.2.3")).toBe("10.20.30");
    expect(firstSemver("1.2 then 3.4.5")).toBe("3.4.5");
    expect(firstSemver("x12.3")).toBeUndefined();
    expect(firstSemver("12.3")).toBeUndefined();
    expect(firstSemver("1.2.")).toBeUndefined();
    expect(firstSemver("")).toBeUndefined();
  });

  it("matches the old regex on a set of tricky inputs", () => {
    const old = (text: string) => /(\d+\.\d+\.\d+)/.exec(text)?.[1];
    for (const text of [
      "1.2.3",
      "..1.2.3..",
      "1.2.a.3.4.5",
      "1.2.3.4.5.6",
      "a1.b2.c3",
      "0.0.0-rc1",
      "1.22.333.4444",
      "7. 8.9",
      "٣.٤.٥ 1.2.3",
    ]) {
      expect(firstSemver(text)).toBe(old(text));
    }
  });

  it("stays linear on 100,000-character adversarial input", () => {
    expect(firstSemver("1".repeat(LONG))).toBeUndefined();
    expect(firstSemver("1..".repeat(LONG))).toBeUndefined();
    expect(firstSemver(`${"1.".repeat(LONG)}2.3`)).toBe("1.1.1");
  });
});

describe("collapseTrailingWhitespace", () => {
  it("replaces trailing whitespace by exactly one space", () => {
    expect(collapseTrailingWhitespace("2026 ")).toBe("2026 ");
    expect(collapseTrailingWhitespace("2026  \t")).toBe("2026 ");
    expect(collapseTrailingWhitespace("2026")).toBe("2026");
    expect(collapseTrailingWhitespace("  ")).toBe(" ");
    expect(collapseTrailingWhitespace("a b")).toBe("a b");
    expect(collapseTrailingWhitespace("")).toBe("");
  });

  it("stays linear on 100,000-character adversarial input", () => {
    const input = `${" ".repeat(LONG)}x`;
    expect(collapseTrailingWhitespace(input)).toBe(input);
    expect(collapseTrailingWhitespace(`x${" ".repeat(LONG)}`)).toBe("x ");
  });
});

describe("compareText", () => {
  it("orders with localeCompare and an explicit en locale", () => {
    expect(["b", "a", "C"].sort(compareText)).toEqual(
      ["b", "a", "C"].sort((a, b) => a.localeCompare(b, "en")),
    );
    expect(compareText("a", "a")).toBe(0);
    expect(compareText("a", "b")).toBeLessThan(0);
  });
});
