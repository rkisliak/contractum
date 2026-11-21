// The card's Notices tab (D-UI-13-02): a read-only, paged feed of notices with time, kind, the
// contract and version, and the per-channel delivery state.
import { Link } from "@tanstack/react-router";
import { Inbox } from "lucide-react";
import type { CardNotice, SubscriberCard } from "@/api/types";
import { Badge, type BadgeIntent } from "@/components/Badge";
import { DataTable, type Column } from "@/components/DataTable";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { useCursorList } from "@/lib/useCursorList";
import { problemOf } from "@/lib/unexpected";
import { cardKeys, useApi } from "./api";
import styles from "./Cards.module.css";

const deliveryIntent: Record<string, BadgeIntent> = {
  delivered: "success",
  pending: "neutral",
  failed: "danger",
  skipped: "neutral",
};

/** A short readable message from the notice payload, when it carries one. */
function messageOf(notice: CardNotice): string {
  const payload = notice.payload;
  for (const key of ["message", "summary", "reason", "note"]) {
    const value = payload[key];
    if (typeof value === "string" && value !== "") return value;
  }
  const semver = payload.semver;
  return typeof semver === "string" ? `Version ${semver}` : "";
}

export function CardNotices({ card }: { card: SubscriberCard }) {
  const api = useApi();
  const list = useCursorList<CardNotice>({
    queryKey: cardKeys.notices(card.id),
    fetchPage: async (cursor) => {
      const { data } = await api.GET(
        "/api/v1/contracts/cards/{card_id}/notices",
        {
          params: {
            path: { card_id: card.id },
            query: { limit: 50, ...(cursor === null ? {} : { cursor }) },
          },
        },
      );
      if (data === undefined) throw new Error("GET /notices had no body");
      return data;
    },
  });

  const columns: Column<CardNotice>[] = [
    {
      id: "time",
      header: "Time",
      rowHeader: true,
      nowrap: true,
      cell: (row) => (
        <time dateTime={row.occurred_at} title={row.occurred_at}>
          {formatDateTime(row.occurred_at)}
        </time>
      ),
    },
    {
      id: "kind",
      header: "Kind",
      cell: (row) => <code className={styles.mono}>{row.event_type}</code>,
    },
    {
      id: "contract",
      header: "Contract",
      cell: (row) => (
        <Link
          to="/contracts/$contractId"
          params={{ contractId: row.contract_id }}
          className={styles.rowLink}
        >
          Open contract
        </Link>
      ),
    },
    { id: "message", header: "Message", cell: (row) => messageOf(row) },
    {
      id: "deliveries",
      header: "Deliveries",
      cell: (row) =>
        row.deliveries.length === 0 ? (
          "In-app only"
        ) : (
          <span className={styles.inline}>
            {row.deliveries.map((delivery) => (
              <Badge
                key={delivery.channel}
                intent={deliveryIntent[delivery.status] ?? "neutral"}
              >
                {`${delivery.channel === "email" ? "Email" : "Webhook"}: ${delivery.status}`}
              </Badge>
            ))}
          </span>
        ),
    },
  ];

  if (list.error)
    return (
      <ProblemAlert problem={problemOf(list.error)} onRetry={list.refetch} />
    );
  if (list.isPending) return <Skeleton label="notices" rows={3} />;
  if (list.rows.length === 0)
    return (
      <EmptyState
        level={3}
        icon={Inbox}
        heading="No notices yet"
        body="Notices appear here when a pinned version is deprecated, superseded, unpublished or its pin is removed."
      />
    );
  return (
    <DataTable
      caption={`Notices for ${card.name}`}
      captionHidden
      compact
      columns={columns}
      rows={list.rows}
      getRowId={(row) => row.id}
      loadMore={{
        hasMore: list.hasMore,
        isLoadingMore: list.isLoadingMore,
        onLoadMore: list.loadMore,
        error: list.pageError,
      }}
    />
  );
}
