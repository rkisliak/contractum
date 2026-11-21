// locatePointer never lets a parser failure reach the screen: when the YAML library throws, the
// finding is listed with "location unavailable" (null) instead. The library is mocked to throw.
import { describe, expect, it, vi } from "vitest";

vi.mock("yaml", async (importOriginal) => {
  const actual = await importOriginal<typeof import("yaml")>();
  return {
    ...actual,
    parseDocument: () => {
      throw new RangeError("Maximum call stack size exceeded");
    },
  };
});

const { locatePointer } = await import("./pointerMap");

describe("locatePointer when the parser throws", () => {
  it("gives null instead of throwing", () => {
    expect(locatePointer("a: 1\n", "/a", "yaml")).toBeNull();
  });
});
