// TextareaField (UI-SPEC 5.2, textarea.md): a native textarea in the field frame. It grows with
// `rows` and the user can resize it vertically. Monospace on request, for YAML and JSON.
import type { ComponentPropsWithRef } from "react";
import { cx } from "@/lib/cx";
import {
  FormField,
  useFormFieldControl,
  type FormFieldProps,
} from "./FormField";
import { FieldShell } from "./TextField";
import styles from "./form.module.css";

type NativeTextareaProps = Omit<
  ComponentPropsWithRef<"textarea">,
  "id" | "className"
>;

export type TextareaFieldProps = Omit<FormFieldProps, "children"> &
  NativeTextareaProps & { monospace?: boolean };

function TextareaInput({
  monospace = false,
  disabled,
  readOnly,
  rows = 4,
  ref,
  ...rest
}: NativeTextareaProps & { monospace?: boolean }) {
  const wiring = useFormFieldControl();
  return (
    <FieldShell disabled={disabled} readOnly={readOnly} multiline>
      <textarea
        {...rest}
        ref={ref}
        rows={rows}
        id={wiring.id}
        aria-describedby={wiring["aria-describedby"]}
        aria-invalid={wiring["aria-invalid"]}
        disabled={disabled}
        readOnly={readOnly}
        className={cx(styles.input, styles.textarea, monospace && styles.mono)}
      />
    </FieldShell>
  );
}

export function TextareaField({
  id,
  label,
  helper,
  error,
  optional,
  ...textareaProps
}: TextareaFieldProps) {
  return (
    <FormField
      id={id}
      label={label}
      helper={helper}
      error={error}
      optional={optional}
    >
      <TextareaInput {...textareaProps} />
    </FormField>
  );
}
