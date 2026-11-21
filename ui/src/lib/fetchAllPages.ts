// Reads a keyset-paginated list to its end for lookups that need every record (the principal
// picker and the principal-name map). The list endpoints have no server search, so the client
// follows `next_cursor` at the largest page size. A cap keeps a runaway list (or a server that
// keeps answering a cursor) from looping forever; `truncated` tells the caller it was hit.
export const PAGE_LIMIT = 100;
/** At most this many pages per list (5,000 records at 100 per page). */
export const MAX_PAGES = 50;

export type Page<T> = { items: T[]; next_cursor?: string | null };

export async function fetchAllPages<T>(
  fetchPage: (query: {
    limit: number;
    cursor?: string;
  }) => Promise<Page<T> | undefined>,
  maxPages: number = MAX_PAGES,
): Promise<{ items: T[]; truncated: boolean }> {
  const items: T[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < maxPages; page += 1) {
    const data = await fetchPage({
      limit: PAGE_LIMIT,
      ...(cursor ? { cursor } : {}),
    });
    items.push(...(data?.items ?? []));
    const next = data?.next_cursor ?? null;
    if (!next || next === cursor) return { items, truncated: false };
    cursor = next;
  }
  return { items, truncated: true };
}
