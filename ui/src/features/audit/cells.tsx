// Shared audit cells: category badge (text, not colour alone), actor, object link to its UI route
// when one exists, and local time with the UTC instant in the title.
import { Link } from "@tanstack/react-router";
import type { AuditEvent } from "@/api/types";
import { Badge, type BadgeIntent } from "@/components/Badge";
export { Breakable } from "@/components/Breakable";
import styles from "./Audit.module.css";

const categoryIntent: Record<string, BadgeIntent> = {
  change: "brand",
  access: "neutral",
  denial: "danger",
};

export function CategoryBadge({ category }: { category: string }) {
  return (
    <Badge intent={categoryIntent[category] ?? "neutral"}>
      {category.charAt(0).toUpperCase() + category.slice(1)}
    </Badge>
  );
}

const instant = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
});

export function formatInstant(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : instant.format(date);
}

const instantDate = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const instantTime = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });

/** The date and the time as two lines for a table cell (each kept whole). */
export function formatInstantParts(value: string): string[] {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return [value];
  return [instantDate.format(date), instantTime.format(date)];
}

export function actorOf(event: AuditEvent): {
  id: string;
  name: string;
  via: string;
} {
  const actor = event.actor;
  const id = typeof actor.id === "string" ? actor.id : "";
  const display =
    typeof actor.display === "string" && actor.display !== ""
      ? actor.display
      : id;
  const via = typeof actor.via === "string" ? actor.via : "";
  return { id, name: display || "System", via };
}

/** A string field of the event's after (else before) snapshot, or undefined. */
function snapshotText(event: AuditEvent, key: string): string | undefined {
  for (const snapshot of [event.after, event.before]) {
    if (snapshot && typeof snapshot === "object" && !Array.isArray(snapshot)) {
      const value = (snapshot as Record<string, unknown>)[key];
      if (typeof value === "string" && value !== "") return value;
    }
  }
  return undefined;
}

export function ObjectCell({ event }: { event: AuditEvent }) {
  const id = event.aggregate_id;
  const label = `${event.aggregate_type} ${id.length > 13 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id}`;
  const title = `${event.aggregate_type} ${id}`;
  const props = { className: styles.link, title, children: label };
  switch (event.aggregate_type) {
    case "contract":
      // A deleted contract has no page to open.
      if (event.action === "contract.deleted") break;
      return (
        <Link
          to="/contracts/$contractId"
          params={{ contractId: id }}
          {...props}
        />
      );
    case "folder":
      return (
        <Link to="/folders/$folderId" params={{ folderId: id }} {...props} />
      );
    case "card":
      return <Link to="/cards/$cardId" params={{ cardId: id }} {...props} />;
    case "rule_set":
      return (
        <Link
          to="/rule-sets/$ruleSetId"
          params={{ ruleSetId: id }}
          {...props}
        />
      );
    case "template":
      return (
        <Link
          to="/templates/$templateId"
          params={{ templateId: id }}
          {...props}
        />
      );
    case "group":
      return (
        <Link to="/admin/groups/$groupId" params={{ groupId: id }} {...props} />
      );
    case "user":
      return (
        <Link to="/admin/users/$userId" params={{ userId: id }} {...props} />
      );
    case "service_account":
      return (
        <Link
          to="/admin/service-accounts/$serviceAccountId"
          params={{ serviceAccountId: id }}
          {...props}
        />
      );
    case "entra_user":
      return (
        <Link
          to="/admin/entra/users/$entraUserId"
          params={{ entraUserId: id }}
          {...props}
        />
      );
    case "entra_registration": {
      // Group and service-principal registrations have pages; a user registration does not.
      const kind = snapshotText(event, "kind");
      if (kind === "group")
        return (
          <Link
            to="/admin/entra/groups/$registrationId"
            params={{ registrationId: id }}
            {...props}
          />
        );
      if (kind === "service_principal")
        return (
          <Link
            to="/admin/entra/service-principals/$registrationId"
            params={{ registrationId: id }}
            {...props}
          />
        );
      break;
    }
    case "grant": {
      // A grant has no page of its own: open the Access tab of its folder.
      const folderId = snapshotText(event, "folder_id");
      if (folderId)
        return (
          <Link
            to="/folders/$folderId/access"
            params={{ folderId }}
            {...props}
          />
        );
      break;
    }
    default:
      break;
  }
  return (
    <span className={styles.idText} title={title}>
      {label}
    </span>
  );
}
