// EmptyState (UI-SPEC 5.9, C-10): heading 18/600, then one sentence with spacing-8 between them
// (a paragraph follows the heading), then the primary action only when the caller passes one
// (callers pass it only when the capability allows it).
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import styles from "./states.module.css";

export type EmptyStateProps = {
  heading: string;
  body: string;
  action?: ReactNode;
  /** Optional 20px outline icon in `body`; decorative. */
  icon?: LucideIcon;
  /** Heading level; h2 by default (the page owns the h1). */
  level?: 2 | 3 | 4;
};

export function EmptyState({
  heading,
  body,
  action,
  icon: Icon,
  level = 2,
}: EmptyStateProps) {
  const Heading = `h${String(level)}` as "h2" | "h3" | "h4";
  return (
    <section className={styles.empty}>
      {Icon ? (
        <Icon
          className={styles.emptyIcon}
          size={20}
          strokeWidth={2}
          aria-hidden="true"
        />
      ) : null}
      <Heading className={styles.emptyHeading}>{heading}</Heading>
      <p className={styles.emptyBody}>{body}</p>
      {action ? <div className={styles.emptyAction}>{action}</div> : null}
    </section>
  );
}
