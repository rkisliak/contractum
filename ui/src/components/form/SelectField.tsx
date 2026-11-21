// SelectField (UI-SPEC 5.2): a native select in the field frame. `appearance: none`, a 16px
// ChevronDown at the trailing inset, the same border, fill and focus ring as a text field.
import { ChevronDown } from "lucide-react";
import type { ComponentPropsWithRef } from "react";
import {
  FormField,
  useFormFieldControl,
  type FormFieldProps,
} from "./FormField";
import { FieldShell, type ControlSize } from "./TextField";
import styles from "./form.module.css";

export type SelectOption = { value: string; label: string; disabled?: boolean };

type NativeSelectProps = Omit<
  ComponentPropsWithRef<"select">,
  "id" | "className" | "size" | "children"
>;

export type SelectFieldProps = Omit<FormFieldProps, "children"> &
  NativeSelectProps & {
    options: SelectOption[];
    /** Shown as the first, empty option ("Any domain"). */
    placeholder?: string;
    controlSize?: ControlSize;
  };

function SelectInput({
  options,
  placeholder,
  controlSize,
  disabled,
  ref,
  ...rest
}: NativeSelectProps & {
  options: SelectOption[];
  placeholder?: string;
  controlSize?: ControlSize;
}) {
  const wiring = useFormFieldControl();
  return (
    <FieldShell controlSize={controlSize} disabled={disabled}>
      <select
        {...rest}
        ref={ref}
        id={wiring.id}
        aria-describedby={wiring["aria-describedby"]}
        aria-invalid={wiring["aria-invalid"]}
        disabled={disabled}
        className={`${styles.input ?? ""} ${styles.select ?? ""}`}
      >
        {placeholder !== undefined ? (
          <option value="">{placeholder}</option>
        ) : null}
        {options.map((option) => (
          <option
            key={option.value}
            value={option.value}
            disabled={option.disabled}
          >
            {option.label}
          </option>
        ))}
      </select>
      <ChevronDown
        className={styles.selectChevron}
        size={16}
        strokeWidth={2}
        aria-hidden="true"
      />
    </FieldShell>
  );
}

export function SelectField({
  id,
  label,
  helper,
  error,
  optional,
  labelHidden,
  ...selectProps
}: SelectFieldProps) {
  return (
    <FormField
      id={id}
      label={label}
      helper={helper}
      error={error}
      optional={optional}
      labelHidden={labelHidden}
    >
      <SelectInput {...selectProps} />
    </FormField>
  );
}
