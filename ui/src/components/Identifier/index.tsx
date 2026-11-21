// Identifier (layout audit): a long machine identifier (ODCS id, URN, UUID) in the normal font
// (never monospace, quick 261119-r5g) on one line. It never splits per character in a table cell: past `max` it ends in an ellipsis and the
// full value is the title (hover) and stays in the accessible name.
import type { CSSProperties } from "react";
import styles from "./Identifier.module.css";

export function Identifier({
  value,
  max = "18rem",
}: {
  value: string;
  /** The widest the identifier may render before it is truncated. */
  max?: string;
}) {
  return (
    <span
      className={styles.id}
      title={value}
      style={{ "--identifier-max": max } as CSSProperties}
    >
      {value}
    </span>
  );
}
