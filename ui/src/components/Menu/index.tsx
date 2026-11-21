// Menu (UI-SPEC 5.10 DropdownMenu): a Radix dropdown with the enterprise dropdown styling. The trigger is any
// element (pass a Button or IconButton); items run `onSelect`, or render a router link with
// `to`. Destructive items sit after a divider in danger text. Inside a Dialog it portals into the
// dialog (C-24).
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { Link } from "@tanstack/react-router";
import type { LucideIcon } from "lucide-react";
import { useId } from "react";
import type { ReactElement, ReactNode } from "react";
import { useDialogContainer } from "@/components/Dialog";
import { Tooltip } from "@/components/Tooltip";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { cx } from "@/lib/cx";
import styles from "./Menu.module.css";

export type MenuItem = {
  label: string;
  icon?: LucideIcon;
  onSelect?: () => void;
  /** A router path; the item becomes a link. */
  to?: string;
  danger?: boolean;
  disabled?: boolean;
  /**
   * Why the item cannot run right now. The item stays reachable by arrow keys and focus (unlike
   * `disabled`), is `aria-disabled`, selecting it does nothing and keeps the menu open, and the
   * reason shows in a tooltip and is exposed through `aria-describedby`.
   */
  unavailableReason?: string;
};

export function Menu({
  trigger,
  items,
  header,
  align = "end",
  side = "bottom",
  tooltip,
}: {
  trigger: ReactElement;
  items: MenuItem[];
  /** Non-interactive header block (the account menu's name and IdP). */
  header?: ReactNode;
  align?: "start" | "end";
  side?: "top" | "right" | "bottom" | "left";
  /** A tooltip on the trigger (opens to the side the menu opens to). */
  tooltip?: string;
}) {
  const container = useDialogContainer();
  const regular = items.filter((item) => !item.danger);
  const danger = items.filter((item) => item.danger);
  return (
    <Dropdown.Root modal={false}>
      {tooltip ? (
        <Tooltip content={tooltip} side={side}>
          <Dropdown.Trigger asChild>{trigger}</Dropdown.Trigger>
        </Tooltip>
      ) : (
        <Dropdown.Trigger asChild>{trigger}</Dropdown.Trigger>
      )}
      <Dropdown.Portal container={container ?? undefined}>
        <Dropdown.Content
          className={styles.content}
          align={align}
          side={side}
          sideOffset={side === "right" ? 12 : 4}
        >
          {header ? (
            <>
              <div className={styles.header}>{header}</div>
              <Dropdown.Separator className={styles.separator} />
            </>
          ) : null}
          {regular.map((item) => (
            <Item key={item.label} item={item} />
          ))}
          {danger.length > 0 && regular.length > 0 ? (
            <Dropdown.Separator className={styles.separator} />
          ) : null}
          {danger.map((item) => (
            <Item key={item.label} item={item} />
          ))}
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

function Item({ item }: { item: MenuItem }) {
  const reasonId = useId();
  const Icon = item.icon;
  const body = (
    <>
      {Icon ? <Icon size={16} strokeWidth={2} aria-hidden="true" /> : null}
      <span>{item.label}</span>
    </>
  );
  const className = cx(styles.item, item.danger && styles.danger);
  if (item.unavailableReason !== undefined) {
    return (
      <Tooltip content={item.unavailableReason} side="bottom" align="end">
        <Dropdown.Item
          className={className}
          aria-disabled="true"
          aria-describedby={reasonId}
          data-unavailable=""
          onSelect={(event) => {
            event.preventDefault();
          }}
        >
          {body}
          <VisuallyHidden id={reasonId}>
            {item.unavailableReason}
          </VisuallyHidden>
        </Dropdown.Item>
      </Tooltip>
    );
  }
  if (item.to !== undefined) {
    return (
      <Dropdown.Item asChild disabled={item.disabled} className={className}>
        <Link to={item.to}>{body}</Link>
      </Dropdown.Item>
    );
  }
  return (
    <Dropdown.Item
      className={className}
      disabled={item.disabled}
      onSelect={() => item.onSelect?.()}
    >
      {body}
    </Dropdown.Item>
  );
}
