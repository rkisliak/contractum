// Alert (UI-SPEC 5.4, C-09, C-23): in-flow, bordered, square and flat. Intent is stated in a
// visually hidden prefix and an icon as well as colour. Danger is `role="alert"`, every other
// intent `role="status"`. Dismiss is optional with a 24x24 hit area.
import type { LucideIcon } from "lucide-react";
import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import type { ReactNode } from "react";
import { IconButton } from "@/components/Button";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { cx } from "@/lib/cx";
import styles from "./Alert.module.css";

export type AlertIntent = "info" | "success" | "warning" | "danger" | "neutral";

const intentIcon: Record<AlertIntent, LucideIcon> = {
  info: Info,
  success: CircleCheck,
  warning: TriangleAlert,
  danger: CircleAlert,
  neutral: Info,
};

const intentPrefix: Record<AlertIntent, string> = {
  info: "Information",
  success: "Success",
  warning: "Warning",
  danger: "Error",
  neutral: "Note",
};

const intentClass: Record<AlertIntent, string | undefined> = {
  info: styles.info,
  success: styles.success,
  warning: styles.warning,
  danger: styles.danger,
  neutral: styles.neutral,
};

export type AlertProps = {
  intent: AlertIntent;
  title?: ReactNode;
  children?: ReactNode;
  /** Renders the Dismiss alert button when given. */
  onDismiss?: () => void;
  /** Optional action row (Small buttons or links). */
  action?: ReactNode;
  className?: string;
};

export function Alert({
  intent,
  title,
  children,
  onDismiss,
  action,
  className,
}: AlertProps) {
  const Icon = intentIcon[intent];
  return (
    <div
      role={intent === "danger" ? "alert" : "status"}
      className={cx(styles.alert, intentClass[intent], className)}
    >
      <Icon
        className={styles.icon}
        size={16}
        strokeWidth={2}
        aria-hidden="true"
      />
      <div className={styles.content}>
        <VisuallyHidden>{intentPrefix[intent]}: </VisuallyHidden>
        {title ? <p className={styles.title}>{title}</p> : null}
        {children ? <div className={styles.body}>{children}</div> : null}
        {action ? <div className={styles.action}>{action}</div> : null}
      </div>
      {onDismiss ? (
        <IconButton
          icon={X}
          size={24}
          aria-label="Dismiss alert"
          className={styles.dismiss}
          onClick={onDismiss}
        />
      ) : null}
    </div>
  );
}
