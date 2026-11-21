// SegmentedControl (button-group.md): a group of Buttons where exactly one option is pressed, for
// a filter that stays on the page (All | Local | Entra). The wrapper is a role=group with the
// label, each option a toggle Button (`aria-pressed`), so keyboard and screen-reader use is the
// same as any button row. Only the wrapper carries the shadow; the buttons overlap by 1px.
import { Button } from "@/components/Button";
import styles from "./SegmentedControl.module.css";

export type SegmentedOption<T extends string> = { value: T; label: string };

export function SegmentedControl<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  /** The accessible name of the group, for example "Source". */
  label: string;
  options: SegmentedOption<T>[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div role="group" aria-label={label} className={styles.group}>
      {options.map((option) => (
        <Button
          key={option.value}
          variant={value === option.value ? "primary" : "outline-neutral"}
          size="small"
          aria-pressed={value === option.value}
          onClick={() => {
            onChange(option.value);
          }}
        >
          {option.label}
        </Button>
      ))}
    </div>
  );
}
