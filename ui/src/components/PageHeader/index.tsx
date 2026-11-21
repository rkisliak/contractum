// PageHeader: the one page frame header every route renders (UI-SPEC section 6, Main). A breadcrumb
// that always starts at Home and always ends at the current page, then the h1 with optional badges
// and the actions on the right, then an optional description. Pages pass only the trail between
// Home and the current page (`breadcrumbs`); Home and the title are added here when missing, so no
// page can drop the breadcrumb. It owns document.title ("{title} · Contractum"). The h1 takes
// tabindex -1 so the shell can move focus to it after a route change. Tab navigation, when a page
// has it, follows the header directly (TabNav), and the shell owns the content width.
import { useEffect, type ReactNode } from "react";
import { Breadcrumb, type Crumb } from "@/components/Breadcrumb";
import styles from "./PageHeader.module.css";

export type PageHeaderProps = {
  title: string;
  /** Overrides the tab title when the h1 is not a good page name ("Welcome, Ada" -> "Home"). */
  documentTitle?: string;
  description?: ReactNode;
  /** The trail between Home and the current page (Home and the current page are added). */
  breadcrumbs?: Crumb[];
  /** The current page's crumb label when it should differ from the h1 ("Welcome, Ada" -> "Home"). */
  crumbLabel?: string;
  /** Badges shown next to the title (kind, Archived). */
  badges?: ReactNode;
  /** The primary action and, at most, a secondary action or an actions menu. */
  actions?: ReactNode;
};

export function PageHeader({
  title,
  documentTitle,
  description,
  breadcrumbs = [],
  crumbLabel,
  badges,
  actions,
}: PageHeaderProps) {
  const trail = frameCrumbs(breadcrumbs, crumbLabel ?? documentTitle ?? title);
  const tabTitle = `${documentTitle ?? title} · Contractum`;
  useEffect(() => {
    document.title = tabTitle;
  }, [tabTitle]);

  return (
    <div className={styles.header}>
      <Breadcrumb items={trail} />
      <div className={styles.row}>
        <div className={styles.titleGroup}>
          <h1 className={styles.title} tabIndex={-1} data-page-heading="">
            {title}
          </h1>
          {badges ? <div className={styles.badges}>{badges}</div> : null}
        </div>
        {actions ? <div className={styles.actions}>{actions}</div> : null}
      </div>
      {description ? (
        <div className={styles.description}>{description}</div>
      ) : null}
    </div>
  );
}

const HOME: Crumb = { label: "Home", to: "/" };

/** Home first, the current page last, whatever the page passed. */
export function frameCrumbs(items: Crumb[], current: string): Crumb[] {
  const trail = [...items];
  if (trail[0]?.to !== "/") trail.unshift(HOME);
  const last = trail[trail.length - 1];
  if (trail.length === 1 && current === HOME.label) return [{ label: "Home" }];
  if (last?.label !== current) trail.push({ label: current });
  return trail;
}
