// "When, by whom" in a table cell: the date (formatDateTime breaks it only between the date and
// the time) followed by the actor, on one line when the column has room and wrapping the actor
// below as a whole when it does not; never run together in one string.
import type { ReactNode } from "react";
import styles from "./DataTable.module.css";

export function WhenBy({ when, by }: { when: string; by: ReactNode }) {
  return (
    <span className={styles.whenBy}>
      <span className={styles.when}>{when}</span>
      <span className={styles.by}>by {by}</span>
    </span>
  );
}
