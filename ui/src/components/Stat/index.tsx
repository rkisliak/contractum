// Stat tiles (enterprise cards.md + icon-shapes.md): a dashboard KPI row. Each tile is a static card
// (a <div> of <dt>/<dd>, as the dl content model allows) with an MD brand icon shape, a 12px uppercase label and a 24px figure. The grid is a <dl>, so the
// label and the value stay programmatically paired.
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./Stat.module.css";

export function StatGrid({
  label,
  children,
}: {
  /** Accessible name of the group, for example "Account summary". */
  label: string;
  children: ReactNode;
}) {
  return (
    <dl className={styles.grid} aria-label={label}>
      {children}
    </dl>
  );
}

export function Stat({
  label,
  value,
  icon: Icon,
  hint,
}: {
  label: string;
  value: ReactNode;
  icon: LucideIcon;
  hint?: ReactNode;
}) {
  return (
    <div className={cx(styles.tile, hint === undefined && styles.noHint)}>
      <dt className={styles.label}>
        <span className={styles.iconShape} aria-hidden="true">
          <Icon size={20} strokeWidth={1.75} />
        </span>
        {label}
      </dt>
      <dd className={styles.value}>{value}</dd>
      {hint === undefined ? null : <dd className={styles.hint}>{hint}</dd>}
    </div>
  );
}
