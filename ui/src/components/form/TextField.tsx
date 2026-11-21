import type { ComponentPropsWithRef, ReactNode } from "react";
import { FormField, useFormFieldControl } from "./FormField";
import type { FormFieldProps } from "./FormField";
import { cx } from "@/lib/cx";
import styles from "./form.module.css";

export type ControlSize = "default" | "small";

/** Shell that owns the border, fill and focus ring; the input and a trailing control sit inside. */
export function FieldShell({
  controlSize = "default",
  disabled,
  readOnly,
  multiline = false,
  children,
}: {
  controlSize?: ControlSize;
  disabled?: boolean;
  readOnly?: boolean;
  /** A textarea: the shell grows with its rows instead of the fixed 36px control height. */
  multiline?: boolean;
  children: ReactNode;
}) {
  const { invalid } = useFormFieldControl();
  return (
    <div
      // Layout hook for the e2e layout checks (fixed 36px control height).
      data-control={multiline ? "multiline" : "single"}
      className={cx(
        styles.shell,
        multiline
          ? styles.shellMultiline
          : controlSize === "small"
            ? styles.shellSmall
            : styles.shellDefault,
        invalid && styles.shellInvalid,
        disabled && styles.shellDisabled,
        readOnly && !disabled && styles.shellReadOnly,
      )}
    >
      {children}
    </div>
  );
}

type InputProps = Omit<
  ComponentPropsWithRef<"input">,
  "id" | "autoComplete" | "className" | "size"
>;

export type TextFieldProps = Omit<FormFieldProps, "children"> &
  InputProps & {
    /** Always set: `username`, `current-password`, `new-password`, `off`, and so on. */
    autoComplete: string;
    controlSize?: ControlSize;
  };

function TextInput({
  autoComplete,
  controlSize,
  disabled,
  readOnly,
  ref,
  ...rest
}: InputProps & { autoComplete: string; controlSize?: ControlSize }) {
  const wiring = useFormFieldControl();
  return (
    <FieldShell
      controlSize={controlSize}
      disabled={disabled}
      readOnly={readOnly}
    >
      <input
        type="text"
        {...rest}
        ref={ref}
        id={wiring.id}
        aria-describedby={wiring["aria-describedby"]}
        aria-invalid={wiring["aria-invalid"]}
        autoComplete={autoComplete}
        disabled={disabled}
        readOnly={readOnly}
        className={styles.input}
      />
    </FieldShell>
  );
}

export function TextField({
  id,
  label,
  helper,
  error,
  optional,
  ...inputProps
}: TextFieldProps) {
  return (
    <FormField
      id={id}
      label={label}
      helper={helper}
      error={error}
      optional={optional}
    >
      <TextInput {...inputProps} />
    </FormField>
  );
}
