// Tabs and TabNav (UI-SPEC 5.10): underline flavour only. `Tabs` switches panels in place (Radix,
// roving tabindex, arrow keys). `TabNav` is a nav of route links, the current one marked
// aria-current="page" by the router. Never mix the two in one list. A TabNav with a single tab
// is not rendered.
import * as RadixTabs from "@radix-ui/react-tabs";
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import styles from "./Tabs.module.css";

export type TabItem = { value: string; label: string; content: ReactNode };

export function Tabs({
  label,
  items,
  value,
  defaultValue,
  onValueChange,
}: {
  /** Accessible name of the tab list. */
  label: string;
  items: TabItem[];
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
}) {
  return (
    <RadixTabs.Root
      className={styles.root}
      value={value}
      defaultValue={defaultValue ?? items[0]?.value}
      onValueChange={onValueChange}
    >
      <RadixTabs.List className={styles.list} aria-label={label}>
        {items.map((item) => (
          <RadixTabs.Trigger
            key={item.value}
            value={item.value}
            className={styles.tab}
          >
            {item.label}
          </RadixTabs.Trigger>
        ))}
      </RadixTabs.List>
      {items.map((item) => (
        <RadixTabs.Content
          key={item.value}
          value={item.value}
          className={styles.panel}
        >
          {item.content}
        </RadixTabs.Content>
      ))}
    </RadixTabs.Root>
  );
}

export type TabNavItem = {
  /** A router path, already resolved ("/folders/abc/contracts"). */
  to: string;
  label: string;
  /** Match the path exactly (the index tab), otherwise any child path matches too. */
  exact?: boolean;
};

export function TabNav({
  label,
  items,
}: {
  label: string;
  items: TabNavItem[];
}) {
  if (items.length < 2) return null;
  return (
    <nav aria-label={label} className={styles.nav}>
      <ul className={styles.list}>
        {items.map((item) => (
          <li key={item.to}>
            <Link
              to={item.to}
              className={styles.tab}
              activeOptions={{
                exact: item.exact ?? false,
                includeSearch: false,
              }}
            >
              {item.label}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
