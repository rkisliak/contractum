// Keyset ("Load more results") lists over the API's `{ items, next_cursor }` pages (D-UI-08-28).
// `fetchPage(cursor)` calls the typed client; the hook flattens the pages for a DataTable.
import { useInfiniteQuery, type QueryKey } from "@tanstack/react-query";

export type CursorPage<T> = { items: T[]; next_cursor: string | null };

export function useCursorList<T, P extends CursorPage<T> = CursorPage<T>>({
  queryKey,
  fetchPage,
  enabled = true,
}: {
  queryKey: QueryKey;
  fetchPage: (cursor: string | null) => Promise<P>;
  enabled?: boolean;
}) {
  const query = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) => fetchPage(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled,
  });
  const rows = query.data?.pages.flatMap((page) => page.items) ?? [];
  return {
    rows,
    /** The first page as returned, for page-level fields such as totals. */
    firstPage: query.data?.pages[0],
    /** True until the first page arrives (render skeleton rows). */
    isPending: query.isPending,
    /** The first page failed: no rows to keep. */
    error: query.isError && query.data === undefined ? query.error : null,
    /** A later page failed: keep the rows, show a compact alert above the footer. */
    pageError: query.isFetchNextPageError ? query.error : null,
    hasMore: query.hasNextPage,
    isLoadingMore: query.isFetchingNextPage,
    loadMore: () => {
      void query.fetchNextPage();
    },
    refetch: () => {
      void query.refetch();
    },
  };
}
