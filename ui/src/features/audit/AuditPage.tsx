// The audit explorer (/audit, D-UI-13-05..07): the filter bar, then a dense keyset table of the
// events the API returns for the caller (newest first, "Load more results"). The UI never
// post-filters and never hints that hidden events exist. A row's time opens the detail drawer,
// whose event id is the `event` search param, so a single event is deep-linkable too.
import { Breakable } from "@/components/Breakable";
import { ScrollText } from "lucide-react";
import { useRouteContext } from "@tanstack/react-router";
import type { AuditEvent } from "@/api/types";
import { Button } from "@/components/Button";
import { DataTable, type Column } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useMe } from "@/features/access/useMe";
import { useCursorList } from "@/lib/useCursorList";
import { problemOf } from "@/lib/unexpected";
import { AuditEventDrawer } from "./AuditEventDrawer";
import { AuditFilters } from "./AuditFilters";
import {
  CategoryBadge,
  ObjectCell,
  actorOf,
  formatInstantParts,
} from "./cells";
import {
  hasFilters,
  toApiQuery,
  type AuditFilters as Filters,
  type AuditSearch,
} from "./query";
import styles from "./Audit.module.css";

export function AuditPage({
  search,
  onSearchChange,
}: {
  search: AuditSearch;
  onSearchChange: (next: AuditSearch) => void;
}) {
  const { api } = useRouteContext({ from: "__root__" });
  const me = useMe();
  const { event, ...filters } = search;
  const query = toApiQuery(filters);

  const list = useCursorList<AuditEvent>({
    queryKey: ["audit", "events", query],
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/audit/events", {
        params: {
          query: {
            ...query,
            limit: 50,
            ...(cursor === null ? {} : { cursor }),
          },
        },
      });
      if (data === undefined) throw new Error("GET /audit/events had no body");
      return data;
    },
  });

  const open = (id: string) => {
    onSearchChange({ ...search, event: id });
  };

  const columns: Column<AuditEvent>[] = [
    {
      id: "time",
      header: "Time",
      rowHeader: true,
      nowrap: true,
      cell: (row) => (
        <button
          type="button"
          className={styles.rowButton}
          title={`${row.occurred_at} (UTC)`}
          onClick={() => {
            open(row.event_id);
          }}
        >
          <time dateTime={row.occurred_at} className={styles.instant}>
            {formatInstantParts(row.occurred_at).join(" ")}
          </time>
        </button>
      ),
    },
    {
      id: "category",
      header: "Category",
      nowrap: true,
      cell: (row) => <CategoryBadge category={row.category} />,
    },
    {
      id: "action",
      header: "Action",
      // Long action ids wrap at their dots so the actor keeps one line at 1440.
      cell: (row) => (
        <span className={styles.actionText}>
          <Breakable text={row.action} />
        </span>
      ),
    },
    {
      id: "actor",
      header: "Actor",
      nowrap: true,
      cell: (row) => {
        const actor = actorOf(row);
        return (
          <span className={styles.actor} title={actor.id}>
            <span>{actor.name}</span>{" "}
            {actor.via ? (
              <span className={styles.via}>via {actor.via}</span>
            ) : null}
          </span>
        );
      },
    },
    {
      id: "object",
      header: "Object",
      cell: (row) => <ObjectCell event={row} />,
    },
  ];

  return (
    <div className={styles.stack}>
      <PageHeader
        title="Audit log"
        breadcrumbs={[]}
        description="Who did what, and when: every change, document read and denied request you are allowed to see."
      />
      <AuditFilters
        filters={filters}
        myPrincipalId={me.data?.principal_id}
        onApply={(next: Filters) => {
          onSearchChange(next);
        }}
      />
      {list.error ? (
        <ProblemAlert problem={problemOf(list.error)} onRetry={list.refetch} />
      ) : list.isPending ? (
        <Skeleton label="audit events" rows={5} />
      ) : list.rows.length === 0 ? (
        <EmptyState
          level={2}
          icon={ScrollText}
          heading="No events you can see match these filters"
          body={
            hasFilters(filters)
              ? "Widen the time range or clear a filter."
              : "Events appear here as people work with folders, contracts and rules."
          }
          action={
            hasFilters(filters) ? (
              <Button
                variant="secondary"
                size="small"
                onClick={() => {
                  onSearchChange({});
                }}
              >
                Clear filters
              </Button>
            ) : undefined
          }
        />
      ) : (
        <DataTable
          caption="Audit events, newest first"
          captionHidden
          compact
          columns={columns}
          rows={list.rows}
          getRowId={(row) => row.event_id}
          loadMore={{
            hasMore: list.hasMore,
            isLoadingMore: list.isLoadingMore,
            onLoadMore: list.loadMore,
            error: list.pageError,
          }}
        />
      )}
      <AuditEventDrawer
        eventId={event}
        onClose={() => {
          onSearchChange(filters);
        }}
      />
    </div>
  );
}
