// replaceTopLevelId never lets a parser failure reach the screen: when the YAML library throws,
// "Put the contract's id back" is simply not offered (null). The library is mocked to throw.
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

const { replaceTopLevelId } = await import("./idEdit");

describe("replaceTopLevelId when the parser throws", () => {
  it("gives null instead of throwing", () => {
    expect(replaceTopLevelId("id: a\n", "yaml", "urn:b")).toBeNull();
  });
});
