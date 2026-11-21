// Avatar (UI-SPEC 5.7): a 32px circle with initials, decorative. The control that wraps it (the
// account menu button) carries the accessible name.
import { cx } from "@/lib/cx";
import styles from "./Avatar.module.css";

/** First letter of the first two words, upper-cased; `?` when there is nothing to show. */
export function initialsOf(name: string): string {
  const letters = name
    .trim()
    .split(/\s+/)
    .filter((word) => word.length > 0)
    .slice(0, 2)
    .map((word) => Array.from(word)[0] ?? "")
    .join("")
    .toUpperCase();
  return letters || "?";
}

export function Avatar({
  name,
  className,
}: {
  name: string;
  className?: string;
}) {
  return (
    <span aria-hidden="true" className={cx(styles.avatar, className)}>
      {initialsOf(name)}
    </span>
  );
}
