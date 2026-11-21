// The navigation panel (Enterprise preview): the product mark (the account menu lives in the icon
// rail, or in the top bar below 1024px),
// the global contract search, then the registry sections (nav.ts, filtered by `/me`) with
// uppercase labels. The folder tree lives on the /folders page (the primary folder navigator),
// not here. It sits beside the icon rail at 1024px and up and inside the start drawer below.
import { LogoMark, Wordmark } from "@/components/Logo";
import { Link, useRouterState } from "@tanstack/react-router";
import { useHeldAccess } from "@/features/access/useHeldAccess";
import { cx } from "@/lib/cx";
import { GlobalSearch } from "./GlobalSearch";
import { isCurrent, visibleSections, type NavEntry } from "./nav";
import styles from "./Sidebar.module.css";

export function Sidebar({
  id,
  onNavigate,
}: {
  id: string;
  onNavigate?: () => void;
}) {
  const access = useHeldAccess();
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  });
  const sections = visibleSections(access.data);

  return (
    <div className={styles.panel}>
      <Link to="/" className={styles.mark} onClick={onNavigate}>
        <LogoMark size={28} />
        <Wordmark />
      </Link>
      <GlobalSearch onSearch={onNavigate} />
      <nav id={id} aria-label="Primary" className={styles.sidebar}>
        {sections.map((section) => (
          <div key={section.id} className={styles.section}>
            {section.label ? (
              <span className={styles.sectionLabel} id={`${id}-${section.id}`}>
                {section.label}
              </span>
            ) : null}
            <ul
              className={styles.list}
              aria-labelledby={
                section.label ? `${id}-${section.id}` : undefined
              }
            >
              {section.entries.map((entry) => (
                <li key={entry.id}>
                  <NavLink
                    entry={entry}
                    current={isCurrent(entry, pathname)}
                    onNavigate={onNavigate}
                  />
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
    </div>
  );
}

function NavLink({
  entry,
  current,
  onNavigate,
}: {
  entry: NavEntry;
  current: boolean;
  onNavigate?: () => void;
}) {
  const Icon = entry.icon;
  return (
    <Link
      to={entry.to}
      className={cx(styles.item, current && styles.current)}
      aria-current={current ? "page" : undefined}
      onClick={onNavigate}
    >
      <Icon
        size={20}
        strokeWidth={1.75}
        aria-hidden="true"
        className={styles.icon}
      />
      <span>{entry.label}</span>
    </Link>
  );
}
