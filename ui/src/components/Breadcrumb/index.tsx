// Breadcrumb (UI-SPEC 5.10): a labelled nav with an ordered list. Every crumb but the last is a
// link; the last is plain text with aria-current="page". A long segment ends in an ellipsis with its full text as the title.
import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";
import styles from "./Breadcrumb.module.css";

export type Crumb = {
  label: string;
  /** A router path such as "/folders/abc"; omitted for the current page. */
  to?: string;
};

export function Breadcrumb({ items }: { items: Crumb[] }) {
  if (items.length === 0) return null;
  return (
    <nav aria-label="Breadcrumb" className={styles.nav}>
      <ol className={styles.list}>
        {items.map((item, index) => {
          const last = index === items.length - 1;
          return (
            <li key={`${String(index)}-${item.label}`} className={styles.item}>
              {last || item.to === undefined ? (
                <span
                  className={last ? styles.current : styles.static}
                  aria-current={last ? "page" : undefined}
                  title={item.label}
                >
                  {item.label}
                </span>
              ) : (
                <Link to={item.to} className={styles.link} title={item.label}>
                  {item.label}
                </Link>
              )}
              {last ? null : (
                <ChevronRight
                  className={styles.separator}
                  size={14}
                  strokeWidth={2}
                  aria-hidden="true"
                />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
