import { Eye, EyeOff } from "lucide-react";
import { useState } from "react";
import { IconButton } from "@/components/Button";
import { FormField, useFormFieldControl } from "./FormField";
import { FieldShell } from "./TextField";
import type { TextFieldProps } from "./TextField";
import { cx } from "@/lib/cx";
import styles from "./form.module.css";

export type PasswordFieldProps = Omit<TextFieldProps, "type">;

function PasswordInput({
  autoComplete,
  controlSize,
  disabled,
  readOnly,
  ref,
  ...rest
}: Omit<PasswordFieldProps, "id" | "label" | "helper" | "error" | "optional">) {
  const wiring = useFormFieldControl();
  const [revealed, setRevealed] = useState(false);
  return (
    <FieldShell
      controlSize={controlSize}
      disabled={disabled}
      readOnly={readOnly}
    >
      <input
        {...rest}
        ref={ref}
        id={wiring.id}
        // Paste is never blocked: no onPaste handler, no preventDefault.
        type={revealed ? "text" : "password"}
        aria-describedby={wiring["aria-describedby"]}
        aria-invalid={wiring["aria-invalid"]}
        autoComplete={autoComplete}
        disabled={disabled}
        readOnly={readOnly}
        className={cx(styles.input, styles.inputWithTrailing)}
      />
      <span className={styles.trailing}>
        <IconButton
          size={32}
          icon={revealed ? EyeOff : Eye}
          aria-label={revealed ? "Hide password" : "Show password"}
          aria-pressed={revealed}
          aria-controls={wiring.id}
          disabled={disabled}
          onClick={() => {
            setRevealed((current) => !current);
          }}
        />
      </span>
    </FieldShell>
  );
}

export function PasswordField({
  id,
  label,
  helper,
  error,
  optional,
  ...inputProps
}: PasswordFieldProps) {
  return (
    <FormField
      id={id}
      label={label}
      helper={helper}
      error={error}
      optional={optional}
    >
      <PasswordInput {...inputProps} />
    </FormField>
  );
}
