// FormField (UI-SPEC 5.2): label above, helper and error below, wired to the control through
// for/id, aria-describedby and aria-invalid. The control reads those wires from context so a
// future select or textarea can reuse the same field frame.
import { CircleAlert } from "lucide-react";
import { createContext, use } from "react";
import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./form.module.css";

type FieldWiring = {
  id: string;
  "aria-describedby": string | undefined;
  "aria-invalid": true | undefined;
  invalid: boolean;
};

const FieldContext = createContext<FieldWiring | null>(null);

/** The id, aria-describedby and aria-invalid a control inside a FormField must carry. */
export function useFormFieldControl(): FieldWiring {
  const wiring = use(FieldContext);
  if (!wiring) {
    throw new Error("A form control must be rendered inside a FormField");
  }
  return wiring;
}

export type FormFieldProps = {
  /** The control's id; the label's `for` points at it. */
  id: string;
  label: string;
  helper?: string;
  /** A message that names the problem and the fix. Marks the control invalid. */
  error?: string;
  /** Marks the field "Optional" in its label. There are no asterisks. */
  optional?: boolean;
  /**
   * Keeps the label for assistive technology only, for a compact control whose purpose the
   * surrounding header makes obvious ("Switch folder" in a page header). Use sparingly.
   */
  labelHidden?: boolean;
  children: ReactNode;
};

export function FormField({
  id,
  label,
  helper,
  error,
  optional = false,
  labelHidden = false,
  children,
}: FormFieldProps) {
  const helperId = helper ? `${id}-helper` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy =
    [helperId, errorId].filter(Boolean).join(" ") || undefined;
  const invalid = Boolean(error);

  return (
    <FieldContext
      value={{
        id,
        "aria-describedby": describedBy,
        "aria-invalid": invalid ? true : undefined,
        invalid,
      }}
    >
      <div className={styles.field} data-field="">
        <label
          htmlFor={id}
          className={cx(
            styles.label,
            invalid && styles.labelInvalid,
            labelHidden && styles.labelHidden,
          )}
        >
          {label}
          {optional ? <span className={styles.optional}> Optional</span> : null}
        </label>
        {children}
        {helper ? (
          <p id={helperId} className={styles.helper}>
            {helper}
          </p>
        ) : null}
        {error ? (
          <p id={errorId} className={styles.error} role="alert">
            <CircleAlert
              className={styles.errorIcon}
              size={16}
              strokeWidth={2}
              aria-hidden="true"
            />
            <span className={styles.errorText}>{error}</span>
          </p>
        ) : null}
      </div>
    </FieldContext>
  );
}
