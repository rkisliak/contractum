// The card's Subscriptions tab (D-UI-13-03): GET .../subscriptions (active by default, or the full
// pin record), each row with the contract, the pinned SemVer plus a Deprecated/sunset badge, who
// pinned it and when, and Move / Unsubscribe for active pins (card:write).
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Plus } from "lucide-react";
import { useState } from "react";
import type { CardSubscription, SubscriberCard } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { SelectField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useMe } from "@/features/access/useMe";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { useCursorList } from "@/lib/useCursorList";
import { problemOf } from "@/lib/unexpected";
import {
  cardKeys,
  endReasonLabels,
  useApi,
  useCardWrites,
  usePublishedVersions,
} from "./api";
import { MoveDialog, SubscribeDialog } from "./SubscribeDialog";
import styles from "./Cards.module.css";
import { PrincipalName } from "@/components/PrincipalName";

type State = "active" | "ended" | "all";

function ContractName({ contractId }: { contractId: string }) {
  const api = useApi();
  const contract = useQuery({
    queryKey: ["contracts", "detail", contractId],
    queryFn: async () => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}",
        { params: { path: { contract_id: contractId } } },
      );
      return data ?? null;
    },
    staleTime: 60_000,
  });
  const label = contract.data?.name ?? contract.data?.odcs_id ?? contractId;
  return (
    <Link
      to="/contracts/$contractId"
      params={{ contractId }}
      className={styles.rowLink}
    >
      {label}
    </Link>
  );
}

function PinnedVersion({ row }: { row: CardSubscription }) {
  const versions = usePublishedVersions(row.contract_id);
  const version = versions.data?.find((v) => v.version_id === row.version_id);
  return (
    <span className={styles.inline}>
      <span className={styles.ident}>{row.semver}</span>
      {version?.deprecated ? (
        <Badge intent="warning">
          {version.sunset_date
            ? `Deprecated, sunset ${version.sunset_date}`
            : "Deprecated"}
        </Badge>
      ) : null}
    </span>
  );
}

export function CardSubscriptions({
  card,
  canWrite,
}: {
  card: SubscriberCard;
  canWrite: boolean;
}) {
  const api = useApi();
  const { unsubscribe } = useCardWrites();
  const [state, setState] = useState<State>("active");
  const [subscribing, setSubscribing] = useState(false);
  const [moving, setMoving] = useState<CardSubscription | null>(null);
  const [ending, setEnding] = useState<CardSubscription | null>(null);
  const archived = card.archived_at !== null;
  const writable = canWrite && !archived;

  const list = useCursorList<CardSubscription>({
    queryKey: cardKeys.subscriptions(card.id, state),
    fetchPage: async (cursor) => {
      const { data } = await api.GET(
        "/api/v1/contracts/cards/{card_id}/subscriptions",
        {
          params: {
            path: { card_id: card.id },
            query: { state, limit: 50, ...(cursor === null ? {} : { cursor }) },
          },
        },
      );
      if (data === undefined) throw new Error("GET /subscriptions had no body");
      return data;
    },
  });

  const me = useMe();
  const columns: Column<CardSubscription>[] = [
    {
      id: "contract",
      header: "Contract",
      rowHeader: true,
      cell: (row) => <ContractName contractId={row.contract_id} />,
    },
    {
      id: "version",
      header: "Pinned version",
      cell: (row) => <PinnedVersion row={row} />,
    },
    {
      id: "by",
      header: "Pinned by",
      // A name when the caller can resolve it ("You" for itself), else the one-line id.
      cell: (row) =>
        row.pinned_by === me.data?.principal_id ? (
          "You"
        ) : (
          <PrincipalName id={row.pinned_by} />
        ),
    },
    {
      id: "at",
      header: "Pinned at",
      sortValue: (row) => row.pinned_at,
      cell: (row) => formatDateTime(row.pinned_at),
    },
    {
      id: "status",
      header: "Status",
      cell: (row) =>
        row.ended_at === null ? (
          <Badge intent="success">Active</Badge>
        ) : (
          <Badge intent="neutral">
            {`${endReasonLabels[row.end_reason ?? ""] ?? "Ended"} ${formatDateTime(row.ended_at)}`}
          </Badge>
        ),
    },
    {
      id: "actions",
      header: "Actions",
      align: "end",
      cell: (row) =>
        writable && row.ended_at === null ? (
          <span className={styles.actions}>
            <Button
              variant="outline-neutral"
              size="small"
              aria-label={`Move pin ${row.semver}`}
              onClick={() => {
                setMoving(row);
              }}
            >
              Move
            </Button>
            <Button
              variant="outline-danger"
              size="small"
              aria-label={`Unsubscribe from ${row.semver}`}
              onClick={() => {
                setEnding(row);
              }}
            >
              Unsubscribe
            </Button>
          </span>
        ) : null,
    },
  ];

  const subscribeButton = writable ? (
    <Button
      variant="primary"
      size="small"
      icon={Plus}
      onClick={() => {
        setSubscribing(true);
      }}
    >
      Subscribe to a contract
    </Button>
  ) : null;

  return (
    <section className={styles.stack} aria-label="Subscriptions">
      <div className={styles.toolbar}>
        <div className={styles.picker}>
          <SelectField
            id="subscriptions-state"
            label="Show"
            controlSize="small"
            options={[
              { value: "active", label: "Active pins" },
              { value: "ended", label: "Ended pins" },
              { value: "all", label: "Full pin record" },
            ]}
            value={state}
            onChange={(event) => {
              setState(event.target.value as State);
            }}
          />
        </div>
        {subscribeButton}
      </div>
      {list.error ? (
        <ProblemAlert problem={problemOf(list.error)} onRetry={list.refetch} />
      ) : list.isPending ? (
        <Skeleton label="subscriptions" rows={3} />
      ) : list.rows.length === 0 ? (
        <EmptyState
          level={3}
          heading={
            state === "active" ? "No active subscriptions" : "No pins here"
          }
          body="Subscribe the card to a published contract version to receive notices when it is deprecated, superseded or unpublished."
        />
      ) : (
        <DataTable
          caption={`Subscriptions of ${card.name}`}
          captionHidden
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
      )}
      <SubscribeDialog
        open={subscribing}
        onOpenChange={setSubscribing}
        cardId={card.id}
        cardName={card.name}
      />
      <MoveDialog
        open={moving !== null}
        onOpenChange={(open) => {
          if (!open) setMoving(null);
        }}
        cardId={card.id}
        subscriptionId={moving?.id ?? ""}
        contractId={moving?.contract_id ?? ""}
        from={moving?.semver ?? ""}
      />
      <ConfirmDialog
        open={ending !== null}
        onOpenChange={(open) => {
          if (!open) setEnding(null);
        }}
        title="Unsubscribe?"
        body={`${card.name} stops receiving notices for version ${ending?.semver ?? ""}. The pin stays in the card's record as ended.`}
        objectName="subscription"
        confirmLabel="Unsubscribe card"
        busyLabel="Unsubscribing…"
        onConfirm={async () => {
          if (ending === null) return;
          await unsubscribe.mutateAsync({
            cardId: card.id,
            subscriptionId: ending.id,
          });
        }}
      />
    </section>
  );
}
