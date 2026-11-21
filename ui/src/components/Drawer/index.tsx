// Drawer (UI-SPEC 5.10): a Radix dialog that slides in from the start (navigation) or the end
// (detail sheets). Focus is trapped, Esc and the scrim close it, focus returns to the trigger.
import * as RadixDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { IconButton } from "@/components/Button";
import { cx } from "@/lib/cx";
import styles from "./Drawer.module.css";

export type DrawerProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  side?: "start" | "end";
  /** 320px by default; "wide" is 640px for content-heavy detail sheets (tables, diffs). */
  width?: "default" | "wide";
  /**
   * Pads the body with the header's 16px inset so content lines up with the title. Defaults to
   * true for detail sheets (end side); a navigation drawer brings its own panel padding.
   */
  padded?: boolean;
  /** Visually hide the title (navigation drawers keep it for screen readers). */
  hideTitle?: boolean;
  children: ReactNode;
};

export function Drawer({
  open,
  onOpenChange,
  title,
  side = "start",
  width = "default",
  hideTitle = false,
  padded,
  children,
}: DrawerProps) {
  const pad = padded ?? side === "end";
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className={styles.overlay} />
        <RadixDialog.Content
          className={cx(
            styles.panel,
            side === "end" ? styles.end : styles.start,
            width === "wide" && styles.wide,
          )}
          aria-describedby={undefined}
        >
          <div className={styles.head}>
            <RadixDialog.Title
              className={cx(styles.title, hideTitle && styles.hiddenTitle)}
            >
              {title}
            </RadixDialog.Title>
            <RadixDialog.Close asChild>
              <IconButton icon={X} size={32} aria-label={`Close ${title}`} />
            </RadixDialog.Close>
          </div>
          <div className={cx(styles.body, pad && styles.padded)}>
            {children}
          </div>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
