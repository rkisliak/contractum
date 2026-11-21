import { describe, expect, it } from "vitest";
import { safeRedirect } from "./redirect";

describe("safeRedirect", () => {
  it.each([
    ["/", "/"],
    ["/folders/abc?tab=access#top", "/folders/abc?tab=access#top"],
    [undefined, "/"],
    ["", "/"],
    ["folders", "/"],
    ["//evil.example/x", "/"],
    ["/\\evil.example", "/"],
    ["https://evil.example", "/"],
    ["javascript:alert(1)", "/"],
    ["/\t/evil.example", "/"],
    ["/ok\nSet-Cookie: x=1", "/"],
    ["/?x=1", "/?x=1"],
    ["/signin?redirect=%2F", "/signin?redirect=%2F"],
    [" /leading-space", "/"],
  ])("maps %j to %j", (input, expected) => {
    expect(safeRedirect(input)).toBe(expected);
  });
});
