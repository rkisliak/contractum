// Checkbox (UI-SPEC 5.2, C-17): the native input, themed with `appearance: none` so no OS control
// shows. 16px box, `brand-medium` border, `brand` fill when checked with a white mark, label
// 14/600 `heading` that toggles the control. The label sits beside the box, so the field frame
// of FormField (label above) is not reused; helper and error text follow the same pattern.
import { Check, CircleAlert } from "lucide-react";
import type { ComponentPropsWithRef, ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./controls.module.css";

type InputProps = Omit<
  ComponentPropsWithRef<"input">,
  "id" | "type" | "className" | "children" | "aria-describedby" | "aria-invalid"
>;

export type CheckboxProps = InputProps & {
  /** The control's id; the label's `for` points at it. */
  id: string;
  label: ReactNode;
  helper?: string;
  /** A message that names the problem and the fix. Marks the control invalid. */
  error?: string;
};

export function Checkbox({
  id,
  label,
  helper,
  error,
  disabled,
  ref,
  ...rest
}: CheckboxProps) {
  const helperId = helper ? `${id}-helper` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy =
    [helperId, errorId].filter(Boolean).join(" ") || undefined;

  return (
    <div className={styles.checkboxField} data-checkbox="">
      <div className={styles.checkboxRow}>
        <span className={styles.checkboxBox}>
          <input
            {...rest}
            ref={ref}
            id={id}
            type="checkbox"
            disabled={disabled}
            aria-describedby={describedBy}
            aria-invalid={error ? true : undefined}
            className={cx(
              styles.checkboxInput,
              error && styles.checkboxInvalid,
            )}
          />
          <Check
            className={styles.checkboxMark}
            size={12}
            strokeWidth={3}
            aria-hidden="true"
          />
        </span>
        <label
          htmlFor={id}
          className={cx(
            styles.checkboxLabel,
            error && styles.checkboxLabelInvalid,
          )}
        >
          {label}
        </label>
      </div>
      {helper ? (
        <p id={helperId} className={styles.checkboxHelper}>
          {helper}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className={styles.checkboxError} role="alert">
          <CircleAlert
            className={styles.checkboxErrorIcon}
            size={16}
            strokeWidth={2}
            aria-hidden="true"
          />
          <span className={styles.checkboxErrorText}>{error}</span>
        </p>
      ) : null}
    </div>
  );
}
