// Tooltip (UI-SPEC 5.10): dark fill, white text, opens after 300ms on hover or at once on focus,
// Esc dismisses. Never the only label of a control: the trigger keeps its own aria-label.
// The shell renders the one TooltipProvider. Inside a dialog it portals into the dialog's content
// node (C-24), so it shows above the modal scrim; elsewhere into the document body.
import * as RadixTooltip from "@radix-ui/react-tooltip";
import type { ReactElement, ReactNode } from "react";
import { useDialogContainer } from "@/components/Dialog/container";
import styles from "./Tooltip.module.css";

export const TooltipProvider = RadixTooltip.Provider;

export function Tooltip({
  content,
  side = "right",
  align = "center",
  children,
}: {
  content: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  align?: "start" | "center" | "end";
  children: ReactElement;
}) {
  const container = useDialogContainer();
  return (
    <RadixTooltip.Root delayDuration={300}>
      <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
      <RadixTooltip.Portal container={container}>
        <RadixTooltip.Content
          className={styles.content}
          side={side}
          align={align}
          sideOffset={8}
          collisionPadding={8}
        >
          {content}
          <RadixTooltip.Arrow className={styles.arrow} />
        </RadixTooltip.Content>
      </RadixTooltip.Portal>
    </RadixTooltip.Root>
  );
}
