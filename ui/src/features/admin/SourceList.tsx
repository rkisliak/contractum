// Building blocks of the three merged admin lists (Users, Groups, Service accounts; quick
// 261119-l30): one table of local and Microsoft Entra records, a Source filter and a search box kept
// in typed search params (so a view can be bookmarked), one "New" menu, the merge of the two keyset
// lists behind it and the member-count lookup of the Groups table. Everything here only shapes the
// UI; the API still checks every request.
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, type LucideIcon } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Alert } from "@/components/Alert";
import { StatusBadge } from "@/components/Badge/status";
import { Button } from "@/components/Button";
import { DataTable, type Column } from "@/components/DataTable";
import { Checkbox, TextField } from "@/components/form";
import { Menu } from "@/components/Menu";
import { ProblemAlert } from "@/components/ProblemAlert";
import { SegmentedControl } from "@/components/SegmentedControl";
import { EmptyState, Skeleton } from "@/components/states";
import { problemOf } from "@/lib/unexpected";
import { idKeys, useApi } from "./shared";
import styles from "./admin.module.css";

export type Source = "local" | "entra";

/** The typed search params of the merged lists. `source` absent means All. */
export type SourceSearch = { source?: Source; q?: string };

const Q_MAX = 200;

/** Route `validateSearch`: `source` is local or entra only, `q` is trimmed and capped. */
export function validateSourceSearch(
  raw: Record<string, unknown>,
): SourceSearch {
  const out: SourceSearch = {};
  if (raw.source === "local" || raw.source === "entra") out.source = raw.source;
  if (typeof raw.q === "string") {
    const q = raw.q.trim().slice(0, Q_MAX);
    if (q !== "") out.q = q;
  }
  return out;
}

/** Which sources a page shows: without Entra there is only the local list (`source=entra` is All). */
export function sourcesShown(
  search: SourceSearch,
  entraAvailable: boolean,
): { local: boolean; entra: boolean } {
  if (!entraAvailable) return { local: true, entra: false };
  return {
    local: search.source !== "entra",
    entra: search.source !== "local",
  };
}

function tidy(next: SourceSearch): SourceSearch {
  const out: SourceSearch = {};
  if (next.source !== undefined) out.source = next.source;
  if (next.q !== undefined && next.q !== "") out.q = next.q;
  return out;
}

/**
 * The toolbar above a merged table: the Source filter (only when Entra is available), the search
 * box bound to `q`, and optionally "Hide deactivated". The text keeps what was typed (a space
 * between words) while the URL holds it trimmed.
 */
export function SourceToolbar({
  id,
  searchLabel,
  entraAvailable,
  search,
  onSearchChange,
  hideDeactivated,
}: {
  id: string;
  /** For example "Search users". */
  searchLabel: string;
  entraAvailable: boolean;
  search: SourceSearch;
  onSearchChange: (next: SourceSearch) => void;
  hideDeactivated?: { checked: boolean; onChange: (checked: boolean) => void };
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const q = search.q ?? "";
  const text = draft !== null && draft.trim() === q ? draft : q;
  const source = entraAvailable ? (search.source ?? "all") : "all";
  return (
    <div className={styles.toolbar}>
      {entraAvailable ? (
        <SegmentedControl
          label="Source"
          value={source}
          options={[
            { value: "all", label: "All" },
            { value: "local", label: "Local" },
            { value: "entra", label: "Entra" },
          ]}
          onChange={(next) => {
            onSearchChange(
              tidy({ ...search, source: next === "all" ? undefined : next }),
            );
          }}
        />
      ) : null}
      <div className={styles.toolbarField}>
        <TextField
          id={`${id}-search`}
          label={searchLabel}
          type="search"
          autoComplete="off"
          controlSize="small"
          maxLength={Q_MAX}
          value={text}
          onChange={(event) => {
            const value = event.target.value;
            setDraft(value);
            onSearchChange(tidy({ ...search, q: value.trim() }));
          }}
        />
      </div>
      {hideDeactivated ? (
        <Checkbox
          id={`${id}-hide-inactive`}
          label="Hide deactivated"
          checked={hideDeactivated.checked}
          onChange={(event) => {
            hideDeactivated.onChange(event.target.checked);
          }}
        />
      ) : null}
    </div>
  );
}

/** The one "New" menu of a merged page; nothing when the caller may create nothing. */
export function NewMenu({
  items,
}: {
  items: { label: string; onSelect: () => void }[];
}) {
  if (items.length === 0) return null;
  return (
    <Menu
      trigger={
        <Button variant="primary" size="small">
          New
          <ChevronDown size={16} strokeWidth={2} aria-hidden="true" />
        </Button>
      }
      items={items}
    />
  );
}

/** The "Source" column of a merged table: a Local or Entra badge, sorted by source. */
export function sourceColumn<T extends { source: Source }>(): Column<T> {
  return {
    id: "source",
    header: "Source",
    sortValue: (row) => row.source,
    cell: (row) => <StatusBadge status={`source-${row.source}`} />,
  };
}

/** The "Owner" column of a merged table: the owner folder's label, sorted by it. */
export function ownerColumn<T extends { ownerFolderId: string }>(lookup: {
  ownerLabel: (id: string) => string;
}): Column<T> {
  return {
    id: "owner",
    header: "Owner",
    sortValue: (row) => lookup.ownerLabel(row.ownerFolderId),
    cell: (row) => lookup.ownerLabel(row.ownerFolderId),
  };
}

/**
 * The items of a merged page's "New" menu: the local create entry and the Entra register entry,
 * each only when its flag is set. `onPick` receives which dialog to open.
 */
export function localEntraMenuItems({
  localLabel,
  entraLabel,
  showLocal,
  showEntra,
  onPick,
}: {
  localLabel: string;
  entraLabel: string;
  showLocal: boolean;
  showEntra: boolean;
  onPick: (dialog: Source) => void;
}): { label: string; onSelect: () => void }[] {
  const items: { label: string; onSelect: () => void }[] = [];
  if (showLocal)
    items.push({
      label: localLabel,
      onSelect: () => {
        onPick("local");
      },
    });
  if (showEntra)
    items.push({
      label: entraLabel,
      onSelect: () => {
        onPick("entra");
      },
    });
  return items;
}

/**
 * One source of a merged list failed while the other loaded: the loaded rows stay and this names
 * the failed source ("Entra groups could not be loaded") with the retry.
 */
export function PartialFailure({
  label,
  error,
  onRetry,
}: {
  label: string;
  error: unknown;
  onRetry: () => void;
}) {
  const problem = problemOf(error);
  return (
    <Alert
      intent="warning"
      title={`${label} could not be loaded`}
      action={
        <Button variant="secondary" size="small" onClick={onRetry}>
          Retry loading
        </Button>
      }
    >
      {problem.kind === "problem" ? <p>{problem.title}</p> : null}
    </Alert>
  );
}

/** What `useCursorList` returns for one source, plus its name and whether it is requested. */
export type ListPart<R> = {
  /** Names the list in "{label} could not be loaded", for example "Entra groups". */
  label: string;
  /** False: the list is never requested and takes no part in the merge. */
  enabled: boolean;
  rows: R[];
  isPending: boolean;
  error: unknown;
  pageError: unknown;
  hasMore: boolean;
  isLoadingMore: boolean;
  loadMore: () => void;
  refetch: () => void;
};

/**
 * Merges the keyset lists of the enabled sources: rows sorted by name, one "Load more" that
 * loads the next page of every list that has one. When every enabled list fails the first error
 * is returned; when only some fail the loaded rows stay and `failed` names the others.
 */
export function useMergedCursorLists<R>(
  parts: ListPart<R>[],
  nameOf: (row: R) => string,
) {
  const active = parts.filter((part) => part.enabled);
  const errored = active.filter((part) => part.error);
  const all = active.length > 0 && errored.length === active.length;
  const rows = active
    .flatMap((part) => part.rows)
    .sort((a, b) =>
      nameOf(a).localeCompare(nameOf(b), undefined, { sensitivity: "base" }),
    );
  return {
    rows,
    /** True until every requested list has answered. */
    isPending: active.some((part) => part.isPending),
    /** Every requested list failed. */
    error: all ? errored[0]?.error : null,
    /** Some lists failed while others loaded. */
    failed: all
      ? []
      : errored.map((part) => ({
          label: part.label,
          error: part.error,
          retry: part.refetch,
        })),
    pageError: active.find((part) => part.pageError)?.pageError ?? null,
    hasMore: active.some((part) => part.hasMore),
    isLoadingMore: active.some((part) => part.isLoadingMore),
    loadMore: () => {
      for (const part of active)
        if (part.hasMore && !part.isLoadingMore) part.loadMore();
    },
    refetch: () => {
      for (const part of errored) part.refetch();
    },
  };
}

// Member counts: the group list carries none, so each local row asks for its members. The
// gateway's default limit is 50 requests a second, so every count request goes through one
// limiter of four at a time, and a group is counted up to COUNT_MAX_PAGES pages of 100.
const COUNT_CONCURRENCY = 4;
const COUNT_PAGE = 100;
const COUNT_MAX_PAGES = 10;

let running = 0;
const waiting: (() => void)[] = [];

async function acquire(): Promise<void> {
  if (running < COUNT_CONCURRENCY) {
    running += 1;
    return;
  }
  // A releasing request hands its slot straight to the next waiter, so the count never exceeds four.
  await new Promise<void>((resolve) => waiting.push(resolve));
}

function release(): void {
  const next = waiting.shift();
  if (next) next();
  else running -= 1;
}

async function limited<T>(task: () => Promise<T>): Promise<T> {
  await acquire();
  try {
    return await task();
  } finally {
    release();
  }
}

/**
 * The member count of a local group. The key sits under `idKeys.members(groupId)`, so a member
 * write that settles that key refreshes it. A failed count is not retried and not an alert.
 */
export function useMemberCount(groupId: string, enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: [...idKeys.members(groupId), "count"],
    enabled,
    retry: false,
    queryFn: async (): Promise<{ count: number; capped: boolean }> => {
      let cursor: string | undefined;
      let count = 0;
      for (let page = 0; page < COUNT_MAX_PAGES; page += 1) {
        const data = await limited(async () => {
          const { data } = await api.GET(
            "/api/v1/identity/groups/{group_id}/members",
            {
              params: {
                path: { group_id: groupId },
                query: { limit: COUNT_PAGE, ...(cursor ? { cursor } : {}) },
              },
            },
          );
          return data;
        });
        count += data?.items.length ?? 0;
        const next = data?.next_cursor ?? null;
        if (!next || next === cursor) return { count, capped: false };
        cursor = next;
      }
      return { count, capped: true };
    },
  });
}

/** A local group's member count as a table cell. */
export function MemberCount({ groupId }: { groupId: string }) {
  const count = useMemberCount(groupId);
  if (count.isError) return <span className={styles.muted}>Not available</span>;
  if (count.data === undefined) return <span className={styles.muted}>…</span>;
  return count.data.capped ? "1,000+" : String(count.data.count);
}

/**
 * The body of a merged page under its header: a skeleton on the first load, the empty state when
 * nothing exists yet, otherwise the toolbar (kept mounted while a source loads, so a filter click
 * never drops the search box) over the table or the reason there is none.
 */
export function MergedListView<R>({
  id,
  plural,
  caption,
  loading,
  entraAvailable,
  search,
  onSearchChange,
  hideDeactivated,
  merged,
  rows,
  columns,
  getRowId,
  empty,
}: {
  id: string;
  /** For example "users": names the skeleton, the search box and the messages. */
  plural: string;
  caption: string;
  /** The caller's own access is still loading. */
  loading: boolean;
  entraAvailable: boolean;
  search: SourceSearch;
  onSearchChange: (next: SourceSearch) => void;
  hideDeactivated?: { checked: boolean; onChange: (checked: boolean) => void };
  merged: ReturnType<typeof useMergedCursorLists<R>>;
  /** The merged rows after the search and "Hide deactivated". */
  rows: R[];
  columns: Column<R>[];
  getRowId: (row: R) => string;
  empty: {
    icon: LucideIcon;
    heading: string;
    body: string;
    /** Only where the screen has no header action of its own: the header "New" is the one entry. */
    action?: ReactNode;
  };
}) {
  const [settled, setSettled] = useState(false);
  if (!settled && !loading && !merged.isPending) setSettled(true);
  if (loading || (merged.isPending && !settled))
    return <Skeleton label={plural} rows={4} />;
  const shown = sourcesShown(search, entraAvailable);
  const noneYet =
    merged.rows.length === 0 &&
    search.q === undefined &&
    (search.source === undefined || !entraAvailable);
  // A source that failed is named below, never hidden behind "none yet".
  if (
    !merged.isPending &&
    !merged.error &&
    merged.failed.length === 0 &&
    noneYet
  )
    return (
      <EmptyState
        icon={empty.icon}
        heading={empty.heading}
        body={empty.body}
        action={empty.action}
      />
    );
  return (
    <>
      <SourceToolbar
        id={id}
        searchLabel={`Search ${plural}`}
        entraAvailable={entraAvailable}
        search={search}
        onSearchChange={onSearchChange}
        hideDeactivated={hideDeactivated}
      />
      {merged.error ? (
        <ProblemAlert
          problem={problemOf(merged.error)}
          onRetry={merged.refetch}
        />
      ) : merged.isPending ? (
        <Skeleton label={plural} rows={4} />
      ) : (
        <>
          {merged.failed.map((failure) => (
            <PartialFailure
              key={failure.label}
              label={failure.label}
              error={failure.error}
              onRetry={failure.retry}
            />
          ))}
          {rows.length === 0 ? (
            <p className={styles.muted}>
              {merged.rows.length === 0 && search.q === undefined
                ? `No ${shown.entra && !shown.local ? "Entra" : "local"} ${plural} yet.`
                : `No ${plural} match the search.`}
            </p>
          ) : (
            <DataTable
              caption={caption}
              captionHidden
              columns={columns}
              rows={rows}
              getRowId={getRowId}
              loadMore={{
                hasMore: merged.hasMore,
                isLoadingMore: merged.isLoadingMore,
                onLoadMore: merged.loadMore,
                error: merged.pageError,
              }}
            />
          )}
        </>
      )}
    </>
  );
}
