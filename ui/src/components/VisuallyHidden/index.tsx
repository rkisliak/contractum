// Screen-reader-only text (UI-SPEC 5.3, C-06): the clip pattern keeps it in the accessibility
// tree; display none and visibility hidden would remove it.
import type { ComponentPropsWithRef } from "react";
import { cx } from "@/lib/cx";
import styles from "./VisuallyHidden.module.css";

export function VisuallyHidden({
  className,
  ...rest
}: ComponentPropsWithRef<"span">) {
  return <span {...rest} className={cx(styles.root, className)} />;
}
