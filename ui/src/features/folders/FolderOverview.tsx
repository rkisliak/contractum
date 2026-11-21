// The folder Overview tab: what the folder is and its state (kind, path, archived, block
// inheritance) plus who created and last changed it.
import type { Folder } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Card } from "@/components/Card";
import { kindLabel } from "@/features/access/roles";
import { folderName } from "./tree";
import styles from "./FolderPages.module.css";
import { PrincipalName } from "@/components/PrincipalName";
import { collapseTrailingWhitespace } from "@/lib/text";

const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

/**
 * The date and the time each stay on one line (no-break spaces inside them), so a narrow table
 * cell wraps "Dec 14, 2026, / 9:24 PM" and never leaves "PM" alone on a line.
 */
export function formatDateTime(value: string | null): string {
  if (value === null) return "Not set";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const parts = dateTime.formatToParts(date);
  return parts
    .map((part, index) =>
      // Only the separator between the date and the time may break.
      part.type === "literal" && parts[index - 1]?.type === "year"
        ? collapseTrailingWhitespace(part.value)
        : part.value.replace(/\s/g, "\u00a0"),
    )
    .join("");
}

export function FolderOverview({ folder }: { folder: Folder }) {
  return (
    <div className={styles.cards}>
      <Card title="Details">
        <dl className={styles.facts}>
          <dt>Name</dt>
          <dd>{folderName(folder)}</dd>
          <dt>Kind</dt>
          <dd>
            <Badge intent="brand">{kindLabel(folder.kind)}</Badge>
          </dd>
          <dt>Slug</dt>
          <dd>
            <span className={styles.ident}>{folder.slug}</span>
          </dd>
          <dt>Path</dt>
          <dd>
            <span className={styles.ident}>
              {folder.path === "" ? "/" : folder.path}
            </span>
          </dd>
          <dt>Folder ID</dt>
          <dd>
            <span className={styles.ident}>{folder.id}</span>
          </dd>
        </dl>
      </Card>
      <Card title="State">
        <dl className={styles.facts}>
          <dt>Status</dt>
          <dd>
            {folder.archived ? (
              <Badge intent="warning">Archived</Badge>
            ) : (
              <Badge intent="success">Active</Badge>
            )}
          </dd>
          {folder.archived ? (
            <>
              <dt>Archived at</dt>
              <dd>{formatDateTime(folder.archived_at)}</dd>
            </>
          ) : null}
          <dt>Inheritance</dt>
          <dd>
            {folder.block_inheritance
              ? "Blocked: roles granted above this folder do not apply here"
              : "Inherited: roles granted above this folder apply here"}
          </dd>
          <dt>Created</dt>
          <dd>
            {formatDateTime(folder.created_at)} by{" "}
            <PrincipalName id={folder.created_by} />
          </dd>
          <dt>Last changed</dt>
          <dd>
            {formatDateTime(folder.updated_at)} by{" "}
            <PrincipalName id={folder.updated_by} />
          </dd>
        </dl>
      </Card>
    </div>
  );
}
