// Dialog (UI-SPEC 5.5, C-09, C-16, C-24): Radix Dialog with the enterprise overlay panel. Sizes 448, 512, 896,
// 1280 and "full" (the viewport minus a 16px margin on every side), a 24x24 close, a focus trap, page scroll lock, aria-labelledby on the title and
// aria-describedby on the description. Dialogs never nest: close the parent, then open the next.
//
// Controlled only, with no Radix Trigger, so the opener is any button. Radix returns focus to a
// Trigger it knows about, so the element focused when the dialog opens is remembered here and
// focused again on close.
import * as RadixDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { IconButton } from "@/components/Button";
import { Tooltip } from "@/components/Tooltip";
import { cx } from "@/lib/cx";
import { DialogContentNode } from "./container";
import styles from "./Dialog.module.css";

export { useDialogContainer } from "./container";

// Cancels Radix's own dismissal (Esc, outside pointer, outside focus).
function keepOpen(event: Event) {
  event.preventDefault();
}

export type DialogSize = 448 | 512 | 896 | 1280 | "full";

const sizeClass: Record<DialogSize, string | undefined> = {
  448: styles.size448,
  512: styles.size512,
  896: styles.size896,
  1280: styles.size1280,
  full: styles.sizeFull,
};

export type DialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The accessible name; wraps when long, never truncates. */
  title: string;
  /** Rendered at the top of the body and linked as aria-describedby. */
  description?: ReactNode;
  size?: DialogSize;
  /** At most two actions: the Secondary one first, then the Primary. */
  footer?: ReactNode;
  children?: ReactNode;
  /** Where the dialog portals; the document body when omitted. */
  container?: HTMLElement | null;
  /**
   * False removes the close button and ignores Esc, a backdrop click and outside focus, for a
   * dialog that must end through its own action (SecretRevealDialog). Default true.
   */
  dismissible?: boolean;
  /** The close button's icon (default X), for example Minimize2 on a full-screen view. */
  closeIcon?: LucideIcon;
  /**
   * The close button's accessible name (default "Close dialog"). When given, the button also
   * gets a tooltip with the same text, because the icon alone does not say what it does.
   */
  closeLabel?: string;
};

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  size = 512,
  footer,
  children,
  container,
  dismissible = true,
  closeIcon = X,
  closeLabel,
}: DialogProps) {
  const [contentNode, setContentNode] = useState<HTMLDivElement | null>(null);
  const opener = useRef<HTMLElement | null>(null);

  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal container={container}>
        <RadixDialog.Overlay className={styles.overlay} />
        <RadixDialog.Content
          ref={setContentNode}
          className={cx(styles.content, sizeClass[size])}
          // Without a description Radix warns unless the attribute is explicitly undefined.
          {...(description ? {} : { "aria-describedby": undefined })}
          onOpenAutoFocus={(event) => {
            // Runs before focus moves in, so this is still the control that opened the dialog.
            opener.current =
              document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
            // A close button with a tooltip is the first focusable element: auto focus on it
            // would pop the tooltip at once, and the first Esc would only dismiss the tooltip.
            // Focus the panel instead (still trapped); Tab reaches the close button next.
            if (closeLabel && dismissible) {
              event.preventDefault();
              contentNode?.focus();
            }
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            opener.current?.focus();
          }}
          onEscapeKeyDown={dismissible ? undefined : keepOpen}
          onPointerDownOutside={dismissible ? undefined : keepOpen}
          onInteractOutside={dismissible ? undefined : keepOpen}
        >
          <DialogContentNode value={contentNode}>
            <div className={styles.header}>
              <RadixDialog.Title className={styles.title}>
                {title}
              </RadixDialog.Title>
              {dismissible ? (
                closeLabel ? (
                  <Tooltip content={closeLabel} side="bottom" align="end">
                    <RadixDialog.Close asChild>
                      <IconButton
                        icon={closeIcon}
                        size={24}
                        aria-label={closeLabel}
                        className={styles.close}
                      />
                    </RadixDialog.Close>
                  </Tooltip>
                ) : (
                  <RadixDialog.Close asChild>
                    <IconButton
                      icon={closeIcon}
                      size={24}
                      aria-label="Close dialog"
                      className={styles.close}
                    />
                  </RadixDialog.Close>
                )
              ) : null}
            </div>
            <div className={styles.body}>
              {description ? (
                <RadixDialog.Description asChild>
                  <div className={styles.description}>{description}</div>
                </RadixDialog.Description>
              ) : null}
              {children}
            </div>
            {footer ? <div className={styles.footer}>{footer}</div> : null}
          </DialogContentNode>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
