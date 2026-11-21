import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { useCursorList, type CursorPage } from "./useCursorList";

type Row = { id: string };
type Page = CursorPage<Row> & { total: number };

function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  };
}

const firstPage: Page = {
  items: [{ id: "a" }, { id: "b" }],
  next_cursor: "c2",
  total: 3,
};
const lastPage: Page = { items: [{ id: "c" }], next_cursor: null, total: 3 };

function pageFor(cursor: string | null): Promise<Page> {
  return Promise.resolve(cursor === null ? firstPage : lastPage);
}

function renderList(
  fetchPage: (cursor: string | null) => Promise<Page>,
  enabled?: boolean,
) {
  return renderHook(
    () =>
      useCursorList<Row, Page>({
        queryKey: ["rows"],
        fetchPage,
        ...(enabled === undefined ? {} : { enabled }),
      }),
    { wrapper: wrapper() },
  );
}

describe("useCursorList", () => {
  it("is pending until the first page arrives, then exposes its rows and page fields", async () => {
    const fetchPage = vi.fn(pageFor);
    const { result } = renderList(fetchPage);
    expect(result.current.isPending).toBe(true);
    expect(result.current.rows).toEqual([]);
    await waitFor(() => {
      expect(result.current.isPending).toBe(false);
    });
    expect(result.current.rows.map((row) => row.id)).toEqual(["a", "b"]);
    expect(result.current.firstPage?.total).toBe(3);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.error).toBeNull();
    expect(fetchPage).toHaveBeenCalledWith(null);
  });

  it("loads the next page by cursor and flattens the rows", async () => {
    const fetchPage = vi.fn(pageFor);
    const { result } = renderList(fetchPage);
    await waitFor(() => {
      expect(result.current.hasMore).toBe(true);
    });
    act(() => {
      result.current.loadMore();
    });
    await waitFor(() => {
      expect(result.current.rows.map((row) => row.id)).toEqual(["a", "b", "c"]);
    });
    expect(fetchPage).toHaveBeenLastCalledWith("c2");
    expect(result.current.hasMore).toBe(false);
    expect(result.current.isLoadingMore).toBe(false);
  });

  it("reports a failed first page as the error, with no rows", async () => {
    const failure = new Error("first page failed");
    const { result } = renderList(() => Promise.reject(failure));
    await waitFor(() => {
      expect(result.current.error).toBe(failure);
    });
    expect(result.current.rows).toEqual([]);
    expect(result.current.pageError).toBeNull();
  });

  it("keeps the rows and reports a failed later page as the page error, then refetches", async () => {
    const failure = new Error("second page failed");
    const fetchPage = vi.fn((cursor: string | null) =>
      cursor === null ? Promise.resolve(firstPage) : Promise.reject(failure),
    );
    const { result } = renderList(fetchPage);
    await waitFor(() => {
      expect(result.current.hasMore).toBe(true);
    });
    act(() => {
      result.current.loadMore();
    });
    await waitFor(() => {
      expect(result.current.pageError).toBe(failure);
    });
    expect(result.current.error).toBeNull();
    expect(result.current.rows.map((row) => row.id)).toEqual(["a", "b"]);

    const callsBefore = fetchPage.mock.calls.length;
    act(() => {
      result.current.refetch();
    });
    await waitFor(() => {
      expect(fetchPage.mock.calls.length).toBeGreaterThan(callsBefore);
    });
    expect(fetchPage.mock.calls[callsBefore]).toEqual([null]);
  });

  it("fetches nothing while disabled", () => {
    const fetchPage = vi.fn(() => Promise.resolve(firstPage));
    const { result } = renderList(fetchPage, false);
    expect(fetchPage).not.toHaveBeenCalled();
    expect(result.current.rows).toEqual([]);
    expect(result.current.isPending).toBe(true);
  });
});
