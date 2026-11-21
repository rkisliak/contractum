// The card page (/cards/$cardId, D-UI-13-02): header with the folder breadcrumb, Archived badge,
// "Audit trail", "Edit card" and "Archive card", then in-page tabs (typed `tab` search param):
// Overview, Members, Subscriptions, Notices, Webhook. Actions are gated on the capabilities at
// the card's folder; the API re-checks every write. A 404 renders NotFound.
import { Archive, History, Pencil } from "lucide-react";
import { useState } from "react";
import type { SubscriberCard } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import type { Crumb } from "@/components/Breadcrumb";
import { Button, ButtonLink } from "@/components/Button";
import { Card } from "@/components/Card";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { NotFound } from "@/components/NotFound";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { SecretRevealDialog } from "@/components/SecretRevealDialog";
import { Skeleton } from "@/components/states";
import { Tabs } from "@/components/Tabs";
import { useCapabilities } from "@/features/access/useCapabilities";
import { holdsAny, useHeldAccess } from "@/features/access/useHeldAccess";
import { useMe } from "@/features/access/useMe";
import { useFolderList } from "@/features/folders/api";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { isNotFound, problemOf } from "@/lib/unexpected";
import { useCard, useCardWrites } from "./api";
import { CardFormDialog } from "./CardFormDialog";
import { CardMembers } from "./CardMembers";
import { CardNotices } from "./CardNotices";
import { CardSubscriptions } from "./CardSubscriptions";
import { CardWebhook } from "./CardWebhook";
import { folderLabel } from "./CardIndex";
import styles from "./Cards.module.css";
import { PrincipalName } from "@/components/PrincipalName";

export const CARD_TABS = [
  "overview",
  "members",
  "subscriptions",
  "notices",
  "webhook",
] as const;
export type CardTab = (typeof CARD_TABS)[number];

const contactKindLabel: Record<string, string> = {
  email: "Email",
  url: "Link",
  text: "Text",
};

function Overview({ card }: { card: SubscriberCard }) {
  return (
    <div className={styles.grid}>
      <Card title="Details">
        <dl className={styles.facts}>
          <dt>Name</dt>
          <dd>{card.name}</dd>
          <dt>Description</dt>
          <dd>{card.description}</dd>
          <dt>Delivery</dt>
          <dd>
            {[
              card.email_enabled ? "Email" : null,
              card.webhook_enabled ? "Webhook" : null,
            ]
              .filter(Boolean)
              .join(", ") || "In-app notices only"}
          </dd>
          <dt>Created</dt>
          <dd>
            {formatDateTime(card.created_at)} by{" "}
            <PrincipalName id={card.created_by} />
          </dd>
          <dt>Last changed</dt>
          <dd>
            {formatDateTime(card.updated_at)} by{" "}
            <PrincipalName id={card.updated_by} />
          </dd>
          {card.archived_at ? (
            <>
              <dt>Archived</dt>
              <dd>
                {formatDateTime(card.archived_at)} by{" "}
                {card.archived_by ? (
                  <PrincipalName id={card.archived_by} />
                ) : (
                  "unknown"
                )}
              </dd>
            </>
          ) : null}
        </dl>
      </Card>
      <Card title="Contacts">
        {card.contacts.length === 0 ? (
          <p className={styles.muted}>No contacts.</p>
        ) : (
          <dl className={styles.facts}>
            {card.contacts.map((contact, index) => (
              <div key={index} className={styles.factRow}>
                <dt>{contact.name ?? contactKindLabel[contact.kind]}</dt>
                <dd>
                  {contact.kind === "email" ? (
                    <a
                      className={styles.rowLink}
                      href={`mailto:${contact.value}`}
                    >
                      {contact.value}
                    </a>
                  ) : contact.kind === "url" &&
                    /^https?:\/\//.test(contact.value) ? (
                    <a
                      className={styles.rowLink}
                      href={contact.value}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {contact.value}
                    </a>
                  ) : (
                    contact.value
                  )}
                </dd>
              </div>
            ))}
          </dl>
        )}
      </Card>
    </div>
  );
}

export function CardPage({
  cardId,
  tab,
  onTabChange,
}: {
  cardId: string;
  tab: CardTab;
  onTabChange: (tab: CardTab) => void;
}) {
  const card = useCard(cardId);
  const caps = useCapabilities(card.data?.folder_id ?? null);
  // The audit trail is only offered to a caller with an audit scope (audit:query).
  const held = useHeldAccess();
  const me = useMe();
  const folders = useFolderList(true);
  const { archive } = useCardWrites();
  const [editing, setEditing] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);

  if (card.isError) {
    return isNotFound(card.error) ? (
      <NotFound back={{ label: "subscriptions", to: "/cards" }} />
    ) : (
      <ProblemAlert
        problem={problemOf(card.error)}
        onRetry={() => void card.refetch()}
      />
    );
  }
  if (card.data === undefined) return <Skeleton label="card" rows={4} />;
  const data = card.data;
  const archived = data.archived_at !== null;
  // card:write and card:manage-secret are principal-level gates resolved per card by the API
  // (D-82, D-104), so /me/capabilities never lists them. Mirror the server rule: the folder's
  // card:manage or the card's creator manage it; members may subscribe; a webhook secret needs an
  // interactive sign-in. The API re-checks every write.
  const principal = me.data?.principal_id;
  const myIds = new Set([
    ...(principal ? [principal] : []),
    ...(me.data?.groups ?? []).map((group) => group.id),
  ]);
  const canManage =
    caps.can("card:manage") ||
    (principal !== undefined && data.created_by === principal);
  const isMember = data.members.some((member) => myIds.has(member.member_id));
  const canWrite = canManage || isMember;
  const canRotate = canManage && me.data?.interactive === true;

  const byId = new Map((folders.data?.folders ?? []).map((f) => [f.id, f]));
  const folder = byId.get(data.folder_id);
  const crumbs: Crumb[] = [{ label: "Subscriptions", to: "/cards" }];
  if (folder)
    crumbs.push({
      label: folderLabel(folder, byId),
      to: `/folders/${folder.id}/cards`,
    });
  crumbs.push({ label: data.name });

  return (
    <div className={styles.stack}>
      <PageHeader
        title={data.name}
        description={data.description}
        breadcrumbs={crumbs}
        badges={
          <>
            <Badge intent="brand">Subscriber card</Badge>
            {archived ? <Badge intent="neutral">Archived</Badge> : null}
          </>
        }
        actions={
          <span className={styles.actions}>
            {held.data && holdsAny(held.data, "audit:query", "audit:read") ? (
              <ButtonLink
                variant="ghost"
                size="small"
                icon={History}
                to="/audit"
                search={{ aggregate_type: "card", aggregate_id: data.id }}
              >
                Audit trail
              </ButtonLink>
            ) : null}
            {canManage && !archived ? (
              <Button
                variant="secondary"
                size="small"
                icon={Pencil}
                onClick={() => {
                  setEditing(true);
                }}
              >
                Edit card
              </Button>
            ) : null}
            {canManage && !archived ? (
              <Button
                variant="outline-danger"
                size="small"
                icon={Archive}
                onClick={() => {
                  setArchiving(true);
                }}
              >
                Archive card
              </Button>
            ) : null}
          </span>
        }
      />
      {archived ? (
        <Alert intent="warning" title="This card is archived">
          <p>It no longer receives notices and its subscriptions have ended.</p>
        </Alert>
      ) : null}
      <Tabs
        label="Card sections"
        value={tab}
        onValueChange={(value) => {
          onTabChange(value as CardTab);
        }}
        items={[
          {
            value: "overview",
            label: "Overview",
            content: <Overview card={data} />,
          },
          {
            value: "members",
            label: `Members (${String(data.members.length)})`,
            content: <CardMembers card={data} canManage={canManage} />,
          },
          {
            value: "subscriptions",
            label: "Subscriptions",
            content: <CardSubscriptions card={data} canWrite={canWrite} />,
          },
          {
            value: "notices",
            label: "Notices",
            content: <CardNotices card={data} />,
          },
          {
            value: "webhook",
            label: "Webhook",
            content: (
              <CardWebhook
                card={data}
                canRotate={canRotate}
                onEdit={
                  canManage
                    ? () => {
                        setEditing(true);
                      }
                    : undefined
                }
              />
            ),
          },
        ]}
      />
      <CardFormDialog
        key={data.updated_at}
        open={editing}
        onOpenChange={setEditing}
        card={data}
        onSecret={setSecret}
      />
      <ConfirmDialog
        open={archiving}
        onOpenChange={setArchiving}
        title={`Archive ${data.name}?`}
        body="It stops receiving notices. Its subscriptions end. An archived card cannot be restored."
        objectName="card"
        confirmLabel="Archive card"
        busyLabel="Archiving…"
        onConfirm={async () => {
          await archive.mutateAsync(data.id);
        }}
      />
      <SecretRevealDialog
        open={secret !== null}
        kind="webhook secret"
        secret={secret ?? ""}
        onClosed={() => {
          setSecret(null);
        }}
      />
    </div>
  );
}
