import { describe, expect, it } from "vitest";
import { fetchAllPages, MAX_PAGES, PAGE_LIMIT } from "./fetchAllPages";

describe("fetchAllPages", () => {
  it("follows next_cursor to the end at the largest page size", async () => {
    const queries: unknown[] = [];
    const pages: Record<
      string,
      { items: number[]; next_cursor: string | null }
    > = {
      "": { items: [1, 2], next_cursor: "b" },
      b: { items: [3], next_cursor: "c" },
      c: { items: [4], next_cursor: null },
    };
    const result = await fetchAllPages((query) => {
      queries.push(query);
      return Promise.resolve(pages[query.cursor ?? ""]);
    });
    expect(result).toEqual({ items: [1, 2, 3, 4], truncated: false });
    expect(queries).toEqual([
      { limit: PAGE_LIMIT },
      { limit: PAGE_LIMIT, cursor: "b" },
      { limit: PAGE_LIMIT, cursor: "c" },
    ]);
  });

  it("stops at the page cap and reports it", async () => {
    let calls = 0;
    const result = await fetchAllPages(() => {
      calls += 1;
      return Promise.resolve({
        items: [calls],
        next_cursor: `c${String(calls)}`,
      });
    }, 3);
    expect(result).toEqual({ items: [1, 2, 3], truncated: true });
    expect(calls).toBe(3);
    expect(MAX_PAGES * PAGE_LIMIT).toBe(5000);
  });

  it("stops when a server repeats the cursor or answers nothing", async () => {
    let calls = 0;
    const repeated = await fetchAllPages(() => {
      calls += 1;
      return Promise.resolve({ items: [calls], next_cursor: "same" });
    });
    expect(repeated).toEqual({ items: [1, 2], truncated: false });
    expect(await fetchAllPages(() => Promise.resolve(undefined))).toEqual({
      items: [],
      truncated: false,
    });
  });

  it("propagates a failed page", async () => {
    await expect(
      fetchAllPages(() => Promise.reject(new Error("boom"))),
    ).rejects.toThrow("boom");
  });
});
