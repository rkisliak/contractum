// DataTable (UI-SPEC 5.8): a native table with a caption, column headers with scope, an optional
// row header, client-side sorting on columns that give `sortValue`, a labelled horizontal scroll
// region, and the keyset footer ("Showing {n} loaded" plus "Load more results"). After more rows
// load, a polite live region says "{k} more loaded" and focus moves to the first new row.
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Button } from "@/components/Button";
import { ProblemAlert } from "@/components/ProblemAlert";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { cx } from "@/lib/cx";
import { problemOf } from "@/lib/unexpected";
import styles from "./DataTable.module.css";

export type Column<T> = {
  id: string;
  header: string;
  cell: (row: T) => ReactNode;
  /** The cell is the row header (`th scope="row"`), usually the name column. */
  rowHeader?: boolean;
  align?: "start" | "end";
  /** Enables client-side sorting on this column. */
  sortValue?: (row: T) => string | number;
  /**
   * Keep the cell on one line (a short value rendered as an element). Plain numbers never wrap;
   * dates from `formatDateTime` already break only between the date and the time.
   */
  nowrap?: boolean;
};

function keepsOneLine(content: ReactNode, nowrap: boolean | undefined) {
  return nowrap ?? typeof content === "number";
}

export type LoadMore = {
  hasMore: boolean;
  isLoadingMore: boolean;
  onLoadMore: () => void;
  /** A failed later page: rows stay, a compact alert offers a retry. */
  error?: unknown;
};

export type DataTableProps<T> = {
  caption: string;
  /** Hide the caption visually (the page heading already names the table). */
  captionHidden?: boolean;
  columns: Column<T>[];
  rows: T[];
  getRowId: (row: T) => string;
  loadMore?: LoadMore;
  compact?: boolean;
};

/** A word this long cannot fit a readable column and wraps anywhere (see the effect above). */
const LONG_WORD = /\S{40,}/u;
/** A cell text longer than this cannot stay on one line even when it has spaces (a row header). */
const LONG_TEXT = 36;
/** Text wider than this on one line also makes a cell breakable (18rem at 16px). */
const WIDE_TEXT_PX = 288;

type Sort = { id: string; direction: "ascending" | "descending" } | null;

export function DataTable<T>({
  caption,
  captionHidden = false,
  columns,
  rows,
  getRowId,
  loadMore,
  compact: compactProp = false,
}: DataTableProps<T>) {
  // Six or more columns use the compact cell padding so a full table fits a desktop content width.
  const compact = compactProp || columns.length >= 6;
  const captionId = useId();
  const [sort, setSort] = useState<Sort>(null);
  const bodyRef = useRef<HTMLTableSectionElement>(null);
  const previousCount = useRef<number | null>(null);
  const awaitingMore = useRef(false);
  const [announcement, setAnnouncement] = useState("");

  const sorted = useMemo(() => {
    if (sort === null) return rows;
    const column = columns.find((candidate) => candidate.id === sort.id);
    const value = column?.sortValue;
    if (value === undefined) return rows;
    const factor = sort.direction === "ascending" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const left = value(a);
      const right = value(b);
      if (typeof left === "number" && typeof right === "number")
        return (left - right) * factor;
      return String(left).localeCompare(String(right)) * factor;
    });
  }, [rows, columns, sort]);

  // A cell that holds a word of 40 or more characters, or more than 36 characters of text (a 200-character name, an email, a path) is
  // marked `data-longword`: only those cells may break anywhere, so one hostile value never
  // widens the table past its region while ordinary columns keep whole words (volume audit V-01).
  // A cell whose text arrives later (a name looked up after the first paint) is marked when it
  // does, so the observer re-reads the body on every change.
  useLayoutEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    const mark = () => {
      const cells = [...body.querySelectorAll("td, th")];
      // Read first, write after: one layout for every measurement.
      const long = cells.map((cell) => {
        const text = cell.textContent;
        if (LONG_WORD.test(text) || text.length > LONG_TEXT) return true;
        // Wide scripts (CJK, emoji) are wide in few characters: measure the text itself.
        const range = document.createRange();
        range.selectNodeContents(cell);
        // (jsdom has no layout and no Range rect.)
        return (
          typeof range.getBoundingClientRect === "function" &&
          range.getBoundingClientRect().width > WIDE_TEXT_PX
        );
      });
      cells.forEach((cell, index) => {
        // A cell once marked stays marked while it exists: wrapping makes it narrower, and
        // un-marking it would make it wide again.
        if (long[index] === true) cell.setAttribute("data-longword", "");
      });
    };
    mark();
    const observer = new MutationObserver(mark);
    observer.observe(body, {
      subtree: true,
      childList: true,
      characterData: true,
    });
    return () => {
      observer.disconnect();
    };
  });

  // After a "Load more", focus the first new row and announce the count.
  useEffect(() => {
    const before = previousCount.current;
    previousCount.current = rows.length;
    if (!awaitingMore.current || before === null || rows.length <= before)
      return;
    awaitingMore.current = false;
    const added = rows.length - before;
    const target = bodyRef.current?.rows.item(before);
    target?.focus();
    // The live region is updated outside render, as the result of a user action completing.
    const timer = window.setTimeout(() => {
      setAnnouncement(`${String(added)} more loaded`);
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
  }, [rows.length]);

  function toggleSort(id: string) {
    setSort((current) => {
      if (current?.id !== id) return { id, direction: "ascending" };
      if (current.direction === "ascending")
        return { id, direction: "descending" };
      return null;
    });
  }

  return (
    <div className={styles.wrapper}>
      <div
        className={styles.scroll}
        role="region"
        aria-labelledby={captionId}
        // A scrollable region must be reachable by keyboard (WCAG 2.1.1).
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
      >
        <table className={cx(styles.table, compact && styles.compact)}>
          <caption
            id={captionId}
            className={captionHidden ? styles.hiddenCaption : styles.caption}
          >
            {caption}
          </caption>
          <thead>
            <tr>
              {columns.map((column) => {
                const active = sort?.id === column.id;
                const ariaSort = active ? sort.direction : undefined;
                return (
                  <th
                    key={column.id}
                    scope="col"
                    aria-sort={
                      column.sortValue ? (ariaSort ?? "none") : undefined
                    }
                    className={cx(column.align === "end" && styles.end)}
                  >
                    {column.sortValue ? (
                      <button
                        type="button"
                        className={styles.sortButton}
                        onClick={() => {
                          toggleSort(column.id);
                        }}
                      >
                        {column.header}
                        <SortIcon state={ariaSort} />
                      </button>
                    ) : (
                      column.header
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody ref={bodyRef}>
            {sorted.map((row) => (
              <tr key={getRowId(row)} tabIndex={-1}>
                {columns.map((column) =>
                  column.rowHeader ? (
                    <th
                      key={column.id}
                      scope="row"
                      className={cx(
                        styles.rowHeader,
                        column.align === "end" && styles.end,
                      )}
                    >
                      {column.cell(row)}
                    </th>
                  ) : (
                    <Cell
                      key={column.id}
                      content={column.cell(row)}
                      end={column.align === "end"}
                      nowrap={column.nowrap}
                    />
                  ),
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {loadMore?.error ? (
        <ProblemAlert
          problem={problemOf(loadMore.error)}
          onRetry={loadMore.onLoadMore}
        />
      ) : null}
      {loadMore ? (
        <div className={styles.footer}>
          <span>
            {loadMore.hasMore
              ? `Showing ${String(rows.length)} loaded`
              : "All results loaded"}
          </span>
          {loadMore.hasMore ? (
            <Button
              variant="secondary"
              size="small"
              busy={loadMore.isLoadingMore}
              busyLabel="Loading more results…"
              onClick={() => {
                awaitingMore.current = true;
                loadMore.onLoadMore();
              }}
            >
              Load more results
            </Button>
          ) : null}
        </div>
      ) : null}
      <VisuallyHidden aria-live="polite">{announcement}</VisuallyHidden>
    </div>
  );
}

function SortIcon({
  state,
}: {
  state: "ascending" | "descending" | undefined;
}) {
  const Icon =
    state === "ascending"
      ? ArrowUp
      : state === "descending"
        ? ArrowDown
        : ArrowUpDown;
  return <Icon size={16} strokeWidth={2} aria-hidden="true" />;
}

/** "Not set" for a missing optional value (UI-SPEC partial state), never a blank cell. */
export function orNotSet(value: ReactNode): ReactNode {
  return value === null || value === undefined || value === "" ? (
    <span className={styles.notSet}>Not set</span>
  ) : (
    value
  );
}

function Cell({
  content,
  end,
  nowrap,
}: {
  content: ReactNode;
  end: boolean;
  nowrap: boolean | undefined;
}) {
  return (
    <td
      className={cx(
        end && styles.end,
        keepsOneLine(content, nowrap) && styles.oneLine,
      )}
    >
      {content}
    </td>
  );
}
