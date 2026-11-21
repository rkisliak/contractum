// Subscribers tab (owner view): the subscriber cards that pin each version, with contacts and the
// pin time. "Remove subscription" ends a card's pin with a reason through the owner-side route,
// using the pin's `subscription_id` from the subscriber view (no read of the card, which an owner
// may not be able to see). The route needs `contract:publish`, so only publishers see the action.
import { WhenBy } from "@/components/DataTable/WhenBy";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Users } from "lucide-react";
import { useState } from "react";
import type { ContractSubscriberVersion } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { DataTable } from "@/components/DataTable";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { useCursorList } from "@/lib/useCursorList";
import { problemOf } from "@/lib/unexpected";
import { contractKeys, useApi, useContract } from "./api";
import styles from "./Contracts.module.css";
import { PrincipalName } from "@/components/PrincipalName";

type Target = {
  subscriptionId: string;
  cardId: string;
  cardName: string;
  versionId: string;
  semver: string;
};

export function ContractSubscribers({ contractId }: { contractId: string }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const contract = useContract(contractId);
  const caps = useCapabilities(contract.data?.folder_id ?? null);
  const [target, setTarget] = useState<Target | null>(null);
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string | undefined>(undefined);
  const [notice, setNotice] = useState<string | null>(null);

  const list = useCursorList<ContractSubscriberVersion>({
    queryKey: contractKeys.subscribers(contractId),
    fetchPage: async (cursor) => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}/subscribers",
        {
          params: {
            path: { contract_id: contractId },
            query: { limit: 50, ...(cursor ? { cursor } : {}) },
          },
        },
      );
      return {
        items: data?.versions ?? [],
        next_cursor: data?.next_cursor ?? null,
      };
    },
  });
  const canRemove = caps.can("contract:publish");

  return (
    <section className={styles.stack} aria-label="Subscribers">
      <p className={styles.muted}>
        Subscriber cards pin versions they depend on and are notified about
        deprecations and new versions.
      </p>
      <p className={styles.status} role="status">
        {notice}
      </p>
      {list.error ? (
        <ProblemAlert problem={problemOf(list.error)} onRetry={list.refetch} />
      ) : list.isPending ? (
        <Skeleton label="subscribers" rows={3} />
      ) : list.rows.length === 0 ? (
        <EmptyState
          icon={Users}
          heading="No subscribers"
          body="No subscriber card pins a version of this contract yet."
        />
      ) : (
        <>
          {list.rows.map((version) => (
            <div key={version.version_id} className={styles.stackTight}>
              <h2 className={styles.sectionHeading}>
                Version <span className={styles.ident}>{version.semver}</span>{" "}
                {version.deprecated ? (
                  <Badge intent="warning">
                    Deprecated
                    {version.sunset_date
                      ? ` · sunset ${version.sunset_date}`
                      : ""}
                  </Badge>
                ) : null}
              </h2>
              <DataTable
                caption={`Cards pinned to ${version.semver}`}
                captionHidden
                compact
                rows={version.cards}
                getRowId={(card) => card.card_id}
                columns={[
                  {
                    id: "card",
                    header: "Card",
                    rowHeader: true,
                    cell: (card) => (
                      <>
                        <Link
                          to="/cards/$cardId"
                          params={{ cardId: card.card_id }}
                          className={styles.rowLink}
                        >
                          {card.name}
                        </Link>
                        {card.description ? (
                          <div className={styles.muted}>{card.description}</div>
                        ) : null}
                      </>
                    ),
                  },
                  {
                    id: "contacts",
                    header: "Contacts",
                    cell: (card) =>
                      card.contacts
                        .map(
                          (contact) =>
                            `${contact.name ? `${contact.name} ` : ""}<${contact.value}>`,
                        )
                        .join(", ") || "None",
                  },
                  {
                    id: "pinned",
                    header: "Pinned",
                    cell: (card) => (
                      <WhenBy
                        when={formatDateTime(card.pinned_at)}
                        by={<PrincipalName id={card.pinned_by} />}
                      />
                    ),
                  },
                  {
                    id: "actions",
                    header: "Actions",
                    align: "end",
                    cell: (card) =>
                      canRemove ? (
                        <Button
                          variant="ghost"
                          size="small"
                          onClick={() => {
                            setReason("");
                            setReasonError(undefined);
                            setTarget({
                              subscriptionId: card.subscription_id,
                              cardId: card.card_id,
                              cardName: card.name,
                              versionId: version.version_id,
                              semver: version.semver,
                            });
                          }}
                        >
                          Remove subscription
                        </Button>
                      ) : null,
                  },
                ]}
              />
            </div>
          ))}
          {list.hasMore ? (
            <div>
              <Button
                variant="secondary"
                size="small"
                busy={list.isLoadingMore}
                busyLabel="Loading…"
                onClick={list.loadMore}
              >
                Load more results
              </Button>
            </div>
          ) : null}
        </>
      )}
      <ConfirmDialog
        open={target !== null}
        onOpenChange={(open) => {
          if (!open) setTarget(null);
        }}
        title={`Remove ${target?.cardName ?? "the card"} from ${target?.semver ?? "this version"}?`}
        body={
          <>
            <p>
              The card stops receiving notices for this contract. Its owners are
              told why.
            </p>
            <TextField
              id="remove-reason"
              autoComplete="off"
              label="Reason"
              maxLength={500}
              value={reason}
              error={reasonError}
              onChange={(event) => {
                setReason(event.target.value);
                setReasonError(undefined);
              }}
            />
          </>
        }
        objectName="subscription"
        confirmLabel="Remove subscription"
        busyLabel="Removing…"
        onConfirm={async () => {
          if (!target) return;
          if (reason.trim() === "") {
            setReasonError("Enter a reason.");
            throw new Error("Enter a reason.");
          }
          await api.POST(
            "/api/v1/contracts/contracts/{contract_id}/subscriptions/{subscription_id}/remove",
            {
              params: {
                path: {
                  contract_id: contractId,
                  subscription_id: target.subscriptionId,
                },
              },
              body: { reason: reason.trim() },
            },
          );
          await queryClient.invalidateQueries({
            queryKey: contractKeys.subscribers(contractId),
          });
          setNotice(
            `${target.cardName} no longer subscribes to ${target.semver}.`,
          );
        }}
      />
    </section>
  );
}
