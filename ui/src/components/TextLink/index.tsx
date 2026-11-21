// Links (UI-SPEC 5.3, C-06). Inline links are underlined at rest; nav links never are.
// External links open in a new tab, isolated from the opener, and say so to screen readers.
import { Link } from "@tanstack/react-router";
import { ExternalLink } from "lucide-react";
import type { ComponentPropsWithRef } from "react";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { cx } from "@/lib/cx";
import styles from "./TextLink.module.css";

type Destination =
  | { to: string; href?: never; external?: never }
  | { href: string; to?: never; external?: boolean };

export type TextLinkProps = Destination & {
  variant?: "inline" | "nav";
} & Omit<ComponentPropsWithRef<"a">, "href">;

export function TextLink({
  to,
  href,
  external = false,
  variant = "inline",
  className,
  children,
  ...rest
}: TextLinkProps) {
  const classes = cx(
    styles.link,
    variant === "inline" ? styles.inline : styles.nav,
    className,
  );

  if (to !== undefined) {
    // The `to` prop is a plain string by contract; the router validates it at runtime.
    return (
      <Link {...rest} to={to} className={classes} data-variant={variant}>
        {children}
      </Link>
    );
  }

  return (
    <a
      {...rest}
      href={href}
      className={classes}
      data-variant={variant}
      {...(external
        ? { target: "_blank", rel: "noopener noreferrer" }
        : undefined)}
    >
      {children}
      {external ? (
        <>
          <ExternalLink
            className={styles.externalIcon}
            size={14}
            strokeWidth={2}
            aria-hidden="true"
          />{" "}
          <VisuallyHidden>(opens in a new tab)</VisuallyHidden>
        </>
      ) : null}
    </a>
  );
}
