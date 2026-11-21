// Skeleton (UI-SPEC 5.9, D-UI-08-26): rows that mirror the final layout. The region is
// aria-busy and carries a visually hidden status "Loading {label}". Never a full-page spinner.
import { VisuallyHidden } from "@/components/VisuallyHidden";
import styles from "./states.module.css";

export type SkeletonProps = {
  /** The thing being loaded, for example "access"; announced as "Loading access". */
  label: string;
  rows?: number;
};

export function Skeleton({ label, rows = 1 }: SkeletonProps) {
  return (
    <div className={styles.skeleton} aria-busy="true">
      <VisuallyHidden role="status">Loading {label}</VisuallyHidden>
      {Array.from({ length: Math.max(1, Math.floor(rows)) }, (_, index) => (
        <div
          key={index}
          className={styles.skeletonRow}
          data-skeleton-row=""
          aria-hidden="true"
        />
      ))}
    </div>
  );
}
