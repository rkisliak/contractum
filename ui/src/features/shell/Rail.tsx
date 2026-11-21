// The icon rail (Enterprise preview): a narrow slate column with one icon per navigation section;
// the section holding the current page is a brand tile. Each item has an aria-label and a tooltip.
// At the bottom: "Versions" (the build of each component), the account menu (avatar; the menu opens to the right), the API docs link and the
// toggle that hides or shows the navigation panel.
import { Link, useRouterState } from "@tanstack/react-router";
import { BookOpen, Info, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { Tooltip } from "@/components/Tooltip";
import { useHeldAccess } from "@/features/access/useHeldAccess";
import { cx } from "@/lib/cx";
import { AccountMenu } from "./AccountMenu";
import { isCurrent, visibleSections } from "./nav";
import styles from "./Rail.module.css";

export function Rail({
  panelOpen,
  onTogglePanel,
  onSignOutFailed,
  onShowVersions,
}: {
  panelOpen: boolean;
  onTogglePanel: () => void;
  onSignOutFailed: () => void;
  onShowVersions: () => void;
}) {
  const access = useHeldAccess();
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  });
  const sections = visibleSections(access.data);
  return (
    <nav aria-label="Sections" className={styles.rail}>
      <ul className={styles.list}>
        {sections.map((section) => {
          const first = section.entries[0];
          if (first === undefined) return null;
          // The single-entry Overview section is named by its page ("Home"), so the rail never
          // shares a name with page tabs such as a contract's "Overview".
          const label =
            section.entries.length === 1 && section.id === "main"
              ? first.label
              : (section.label ?? first.label);
          const current = section.entries.some((entry) =>
            isCurrent(entry, pathname),
          );
          const Icon = first.icon;
          return (
            <li key={section.id}>
              <Tooltip content={label}>
                <Link
                  to={first.to}
                  aria-label={label}
                  aria-current={current ? "true" : undefined}
                  className={cx(styles.item, current && styles.current)}
                >
                  <Icon size={20} strokeWidth={1.75} aria-hidden="true" />
                </Link>
              </Tooltip>
            </li>
          );
        })}
      </ul>
      <div className={styles.bottom}>
        <Tooltip content="Versions">
          <button
            type="button"
            className={styles.item}
            aria-label="Versions"
            aria-haspopup="dialog"
            onClick={onShowVersions}
          >
            <Info size={20} strokeWidth={1.75} aria-hidden="true" />
          </button>
        </Tooltip>
        <AccountMenu
          variant="rail"
          onSignOutFailed={onSignOutFailed}
          onShowVersions={onShowVersions}
        />
        <Tooltip content="API docs">
          <a
            href="/api/v1/docs"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="API docs (opens in a new tab)"
            className={styles.item}
          >
            <BookOpen size={20} strokeWidth={1.75} aria-hidden="true" />
          </a>
        </Tooltip>
        <Tooltip
          content={
            panelOpen ? "Hide navigation panel" : "Show navigation panel"
          }
        >
          <button
            type="button"
            className={styles.item}
            aria-label={panelOpen ? "Collapse navigation" : "Expand navigation"}
            aria-expanded={panelOpen}
            aria-controls="primary-panel"
            onClick={onTogglePanel}
          >
            {panelOpen ? (
              <PanelLeftClose size={20} strokeWidth={1.75} aria-hidden="true" />
            ) : (
              <PanelLeftOpen size={20} strokeWidth={1.75} aria-hidden="true" />
            )}
          </button>
        </Tooltip>
      </div>
    </nav>
  );
}
