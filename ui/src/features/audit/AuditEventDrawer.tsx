// The audit event detail (D-UI-13-07), loaded through GET /audit/events/{id}: the envelope, the
// RFC 6902 changes as a readable list (op, path, old and new value), the before/after snapshots
// side by side, and for access and denial events the `detail` object as key/value pairs. A
// `contract.deleted` event offers "Restore contract" to holders of `contract:restore`.
import { useQuery } from "@tanstack/react-query";
import type { AuditEvent } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { Drawer } from "@/components/Drawer";
import { NotFound } from "@/components/NotFound";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { useRouteContext } from "@tanstack/react-router";
import { isNotFound, problemOf } from "@/lib/unexpected";
import { describeChanges, formatValue, opLabels } from "./changes";
import { CategoryBadge, ObjectCell, actorOf, formatInstant } from "./cells";
import { RestoreContract } from "./RestoreContract";
import styles from "./Audit.module.css";

function ChangeList({ event }: { event: AuditEvent }) {
  const rows = describeChanges(event.changes, event.before);
  return (
    <section className={styles.section} aria-labelledby="audit-changes">
      <h3 id="audit-changes" className={styles.sectionTitle}>
        Changes ({String(rows.length)})
      </h3>
      <div className={styles.scroll}>
        <table className={styles.changeTable}>
          <thead>
            <tr>
              <th scope="col">Change</th>
              <th scope="col">Path</th>
              <th scope="col">Old value</th>
              <th scope="col">New value</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={`${row.path}-${String(index)}`}>
                <td>{opLabels[row.op] ?? row.op}</td>
                <th scope="row" className={styles.mono}>
                  {row.from ? `${row.from} → ${row.path}` : row.path || "/"}
                </th>
                <td className={styles.oldValue}>
                  {row.hasOld ? (
                    <code>{formatValue(row.oldValue)}</code>
                  ) : (
                    <span className={styles.none}>None</span>
                  )}
                </td>
                <td className={styles.newValue}>
                  {row.hasNew ? (
                    <code>{formatValue(row.newValue)}</code>
                  ) : (
                    <span className={styles.none}>None</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Snapshots({ event }: { event: AuditEvent }) {
  return (
    <section className={styles.section} aria-labelledby="audit-snapshots">
      <h3 id="audit-snapshots" className={styles.sectionTitle}>
        Before and after
      </h3>
      <div className={styles.sideBySide}>
        <div>
          <h4 className={styles.paneTitle}>Before</h4>
          {/* A scrollable region must be keyboard-focusable (axe scrollable-region-focusable). */}
          <pre
            className={styles.json}
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable region
            tabIndex={0}
            role="region"
            aria-label="Before snapshot"
          >
            {event.before
              ? JSON.stringify(event.before, null, 2)
              : "None (created)"}
          </pre>
        </div>
        <div>
          <h4 className={styles.paneTitle}>After</h4>
          <pre
            className={styles.json}
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable region
            tabIndex={0}
            role="region"
            aria-label="After snapshot"
          >
            {event.after
              ? JSON.stringify(event.after, null, 2)
              : "None (removed)"}
          </pre>
        </div>
      </div>
    </section>
  );
}

function Detail({ detail }: { detail: Record<string, unknown> }) {
  return (
    <section className={styles.section} aria-labelledby="audit-detail">
      <h3 id="audit-detail" className={styles.sectionTitle}>
        Detail
      </h3>
      <dl className={styles.facts}>
        {Object.entries(detail).map(([key, value]) => (
          <div key={key} className={styles.factRow}>
            <dt>{key}</dt>
            <dd className={styles.mono}>{formatValue(value) || "—"}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function EventBody({ event }: { event: AuditEvent }) {
  const actor = actorOf(event);
  return (
    <div className={styles.detailStack}>
      <div className={styles.inline}>
        <CategoryBadge category={event.category} />
        <code className={styles.mono}>{event.action}</code>
        <CopyButton
          value={JSON.stringify(event, null, 2)}
          label="Copy event JSON"
          copiedLabel="Event JSON copied"
        />
      </div>
      <dl className={styles.facts}>
        <dt>Occurred</dt>
        <dd>
          <time dateTime={event.occurred_at}>
            {formatInstant(event.occurred_at)}
          </time>{" "}
          <span className={styles.utc}>({event.occurred_at})</span>
        </dd>
        <dt>Received</dt>
        <dd>{formatInstant(event.received_at)}</dd>
        <dt>Actor</dt>
        <dd>
          {actor.name}
          {actor.via ? ` via ${actor.via}` : ""}
          {actor.id && actor.id !== actor.name ? (
            <span className={styles.secondary}>{actor.id}</span>
          ) : null}
        </dd>
        <dt>Object</dt>
        <dd>
          <ObjectCell event={event} />
          {event.aggregate_version !== null
            ? ` (version ${String(event.aggregate_version)})`
            : ""}
        </dd>
        <dt>Service</dt>
        <dd>{event.service}</dd>
        <dt>Request id</dt>
        <dd className={styles.ident}>{event.request_id ?? "Not set"}</dd>
        <dt>Event id</dt>
        <dd className={styles.ident}>{event.event_id}</dd>
      </dl>
      {event.action === "contract.deleted" ? (
        <RestoreContract event={event} />
      ) : null}
      {event.changes.length > 0 ? <ChangeList event={event} /> : null}
      {event.before || event.after ? <Snapshots event={event} /> : null}
      {event.detail && Object.keys(event.detail).length > 0 ? (
        <Detail detail={event.detail} />
      ) : null}
    </div>
  );
}

export function AuditEventDrawer({
  eventId,
  onClose,
}: {
  eventId: string | undefined;
  onClose: () => void;
}) {
  const { api } = useRouteContext({ from: "__root__" });
  const event = useQuery({
    queryKey: ["audit", "event", eventId],
    enabled: eventId !== undefined,
    queryFn: async (): Promise<AuditEvent> => {
      const { data } = await api.GET("/api/v1/audit/events/{event_id}", {
        params: { path: { event_id: eventId ?? "" } },
      });
      if (data === undefined) throw new Error("GET /events/{id} had no body");
      return data;
    },
  });
  return (
    <Drawer
      open={eventId !== undefined}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="Audit event"
      side="end"
      width="wide"
    >
      {event.isError ? (
        isNotFound(event.error) ? (
          <NotFound nested />
        ) : (
          <ProblemAlert
            problem={problemOf(event.error)}
            onRetry={() => void event.refetch()}
          />
        )
      ) : event.data ? (
        <EventBody event={event.data} />
      ) : (
        <Skeleton label="audit event" rows={5} />
      )}
    </Drawer>
  );
}
