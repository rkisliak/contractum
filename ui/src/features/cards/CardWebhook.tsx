// The card's Webhook tab (D-UI-13-02): delivery settings at a glance and "Rotate secret"
// (card:manage-secret). The API keeps the previous secret valid for an overlap window (D-98), so
// the confirmation says so; the new secret is shown once in SecretRevealDialog and never cached.
import { RotateCw } from "lucide-react";
import { useState } from "react";
import type { SubscriberCard } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { SecretRevealDialog } from "@/components/SecretRevealDialog";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { useCardWrites } from "./api";
import styles from "./Cards.module.css";

export function CardWebhook({
  card,
  canRotate,
  onEdit,
}: {
  card: SubscriberCard;
  canRotate: boolean;
  onEdit?: () => void;
}) {
  const { rotateSecret } = useCardWrites();
  const [confirming, setConfirming] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [overlapUntil, setOverlapUntil] = useState<string | null>(null);
  const archived = card.archived_at !== null;

  return (
    <div className={styles.stack}>
      <Card title="Webhook delivery">
        <dl className={styles.facts}>
          <dt>Status</dt>
          <dd>
            {card.webhook_enabled ? (
              <Badge intent="success">Enabled</Badge>
            ) : (
              <Badge intent="neutral">Off</Badge>
            )}
          </dd>
          <dt>URL</dt>
          <dd>
            {card.webhook_url ? (
              <code className={styles.mono}>{card.webhook_url}</code>
            ) : (
              "Not set"
            )}
          </dd>
          <dt>Signing secret</dt>
          <dd>
            {card.webhook_secret_configured ? "Configured" : "Not created yet"}
          </dd>
        </dl>
        <div className={styles.actions}>
          {onEdit && !archived ? (
            <Button variant="secondary" size="small" onClick={onEdit}>
              Edit delivery settings
            </Button>
          ) : null}
          {canRotate && !archived ? (
            <Button
              variant="outline-danger"
              size="small"
              icon={RotateCw}
              onClick={() => {
                setConfirming(true);
              }}
            >
              Rotate secret
            </Button>
          ) : null}
        </div>
      </Card>
      {overlapUntil ? (
        <Alert intent="info" title="Previous secret still accepted">
          <p>
            The previous secret keeps validating deliveries until{" "}
            {formatDateTime(overlapUntil)}. Update the receiver before then.
          </p>
        </Alert>
      ) : null}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Rotate the webhook secret?"
        body="A new signing secret is created and shown once. The previous secret stays valid only for the overlap window, so update the receiver promptly."
        objectName="current secret"
        confirmLabel="Rotate secret"
        busyLabel="Rotating…"
        onConfirm={async () => {
          const rotated = await rotateSecret.mutateAsync(card.id);
          setOverlapUntil(rotated.previous_valid_until);
          setSecret(rotated.webhook_secret);
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
