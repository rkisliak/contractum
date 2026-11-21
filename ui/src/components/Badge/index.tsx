// Badge (UI-SPEC 5.7): Large only, bordered, square, inline, never full width. The label text is
// always rendered; the optional 14px icon is decoration.
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./Badge.module.css";

export type BadgeIntent =
  "brand" | "neutral" | "success" | "warning" | "danger";

const intentClass: Record<BadgeIntent, string | undefined> = {
  brand: styles.brand,
  neutral: styles.neutral,
  success: styles.success,
  warning: styles.warning,
  danger: styles.danger,
};

export type BadgeProps = {
  intent: BadgeIntent;
  icon?: LucideIcon;
  /** Monospace label, for problem slugs and identifiers. */
  mono?: boolean;
  children: ReactNode;
};

export function Badge({
  intent,
  icon: Icon,
  mono = false,
  children,
}: BadgeProps) {
  return (
    <span
      data-badge=""
      className={cx(styles.badge, intentClass[intent], mono && styles.mono)}
    >
      {Icon ? (
        <Icon
          className={styles.icon}
          size={14}
          strokeWidth={2}
          aria-hidden="true"
        />
      ) : null}
      <span
        className={styles.label}
        title={typeof children === "string" ? children : undefined}
      >
        {children}
      </span>
    </span>
  );
}
