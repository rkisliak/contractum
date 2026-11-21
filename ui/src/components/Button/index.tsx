// Button and IconButton (UI-SPEC 5.1, C-07, C-12, C-25). Native button first; the label never
// wraps or shrinks, and a busy button keeps its width, stays focusable and ignores clicks.
import { Link, type LinkComponentProps } from "@tanstack/react-router";
import type { LucideIcon } from "lucide-react";
import { Loader } from "lucide-react";
import type { ComponentPropsWithRef, MouseEvent, ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./Button.module.css";

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "danger"
  | "ghost"
  | "outline-neutral"
  | "outline-brand"
  | "outline-danger";

export type ButtonSize = "small" | "large";

const variantClass: Record<ButtonVariant, string | undefined> = {
  primary: styles.primary,
  secondary: styles.secondary,
  danger: styles.danger,
  ghost: styles.ghost,
  "outline-neutral": styles.outlineNeutral,
  "outline-brand": styles.outlineBrand,
  "outline-danger": styles.outlineDanger,
};

const sizeClass: Record<ButtonSize, string | undefined> = {
  small: styles.small,
  large: styles.large,
};

export type ButtonProps = {
  variant: ButtonVariant;
  size: ButtonSize;
  /** Shows the spinner and the busy label; the button stays focusable and ignores clicks. */
  busy?: boolean;
  /** The active verb plus an ellipsis, for example "Signing in…". */
  busyLabel?: string;
  fullWidth?: boolean;
  icon?: LucideIcon;
} & ComponentPropsWithRef<"button">;

export function Button({
  variant,
  size,
  busy = false,
  busyLabel,
  fullWidth = false,
  icon: Icon,
  className,
  children,
  type = "button",
  onClick,
  ref,
  ...rest
}: ButtonProps) {
  function handleClick(event: MouseEvent<HTMLButtonElement>) {
    if (busy) {
      // Not `disabled`: the button must stay focusable. A busy submit button must not resubmit.
      event.preventDefault();
      return;
    }
    onClick?.(event);
  }

  const label = (
    <>
      {Icon ? (
        <Icon
          className={styles.icon}
          size={16}
          strokeWidth={2}
          aria-hidden="true"
        />
      ) : null}
      {typeof children === "string" ? (
        // A long text label (a verb plus a 200-character name) ends in an ellipsis inside the
        // button's cap; the full text is the title (volume audit V-11, R15, R16).
        <span className={styles.text} title={children}>
          {children}
        </span>
      ) : (
        children
      )}
    </>
  );

  return (
    <button
      {...rest}
      ref={ref}
      type={type}
      data-button={size}
      aria-busy={busy ? true : undefined}
      className={cx(
        styles.button,
        variantClass[variant],
        sizeClass[size],
        fullWidth && styles.fullWidth,
        className,
      )}
      onClick={handleClick}
    >
      <span className={styles.contents}>
        {/* Both layers stay in the grid cell so the busy label never changes the width. */}
        <span
          className={cx(styles.layer, busy && busyLabel && styles.hidden)}
          aria-hidden={busy && busyLabel ? true : undefined}
        >
          {busy && !busyLabel ? <Spinner /> : label}
        </span>
        {busyLabel ? (
          <span
            className={cx(styles.layer, !busy && styles.hidden)}
            aria-hidden={busy ? undefined : true}
          >
            <Spinner />
            {busyLabel}
          </span>
        ) : null}
      </span>
    </button>
  );
}

function Spinner() {
  return (
    <Loader
      className={cx(styles.icon, styles.spinner)}
      size={16}
      strokeWidth={2}
      aria-hidden="true"
    />
  );
}

export type IconButtonProps = {
  /** The accessible name; mandatory because the button has no visible text. */
  "aria-label": string;
  icon: LucideIcon;
  /** Hit area in px: 24 floor, 32 table row and in-field, 36 toolbar. */
  size: 24 | 32 | 36;
  variant?: ButtonVariant;
} & Omit<ComponentPropsWithRef<"button">, "aria-label" | "children">;

const iconSizeClass: Record<IconButtonProps["size"], string | undefined> = {
  24: styles.icon24,
  32: styles.icon32,
  36: styles.icon36,
};

export function IconButton({
  icon: Icon,
  size,
  variant = "ghost",
  className,
  type = "button",
  ref,
  ...rest
}: IconButtonProps) {
  return (
    <button
      {...rest}
      ref={ref}
      type={type}
      data-button={`icon-${String(size)}`}
      className={cx(
        styles.button,
        styles.iconOnly,
        variantClass[variant],
        iconSizeClass[size],
        className,
      )}
    >
      <Icon size={16} strokeWidth={2} aria-hidden="true" />
    </button>
  );
}

export type ButtonLinkProps = Omit<LinkComponentProps, "children"> & {
  variant: ButtonVariant;
  size: ButtonSize;
  icon?: LucideIcon;
  children: ReactNode;
};

/**
 * A router link that looks and sizes exactly like a Button, for navigation that sits in a row of
 * buttons (a toolbar): same height, padding, icon gap and centring, so the row stays level.
 */
export function ButtonLink({
  variant,
  size,
  icon: Icon,
  className,
  children,
  ...rest
}: ButtonLinkProps) {
  return (
    <Link
      {...rest}
      data-button={size}
      className={cx(
        styles.button,
        variantClass[variant],
        sizeClass[size],
        className,
      )}
    >
      <span className={styles.contents}>
        <span className={styles.layer}>
          {Icon ? (
            <Icon
              className={styles.icon}
              size={16}
              strokeWidth={2}
              aria-hidden="true"
            />
          ) : null}
          {children}
        </span>
      </span>
    </Link>
  );
}

export type IconButtonLinkProps = Omit<
  LinkComponentProps,
  "children" | "aria-label"
> & {
  /** The accessible name; mandatory because the link has no visible text. */
  "aria-label": string;
  icon: LucideIcon;
  size: 24 | 32 | 36;
  variant?: ButtonVariant;
};

/** A router link drawn exactly like an IconButton (row quick actions that navigate). */
export function IconButtonLink({
  icon: Icon,
  size,
  variant = "ghost",
  className,
  ...rest
}: IconButtonLinkProps) {
  return (
    <Link
      {...rest}
      data-button={`icon-${String(size)}`}
      className={cx(
        styles.button,
        styles.iconOnly,
        variantClass[variant],
        iconSizeClass[size],
        className,
      )}
    >
      <Icon size={16} strokeWidth={2} aria-hidden="true" />
    </Link>
  );
}
