// The submit and per-field reset handlers the Governance and Security tabs share (quick
// 261121-0sh). Both tabs build the section's values from their form, show the field errors the
// build found instead of sending, and reset a field by saving the stored values without its keys.
import type { SubmitEvent } from "react";
import type { SettingsSection } from "@/api/types";

/** What a tab's `build` returns: the values to save, or the field errors that block the save. */
export type Built<E> =
  | { values: Record<string, unknown>; errors?: undefined }
  | { errors: E; values?: undefined };

export function sectionHandlers<F, E>({
  section,
  form,
  build,
  setErrors,
  submit,
  savedMessage,
}: {
  section: SettingsSection;
  form: F;
  build: (form: F, section: SettingsSection) => Built<E>;
  setErrors: (errors: E) => void;
  submit: (values: Record<string, unknown>, message: string) => void;
  /** Shown after a successful save from the form's own button. */
  savedMessage: string;
}) {
  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const built = build(form, section);
    if (built.values === undefined) {
      setErrors(built.errors);
      return;
    }
    submit(built.values, savedMessage);
  }

  function resetField(keys: string[], message: string) {
    const next = Object.fromEntries(
      Object.entries(section.values).filter(([key]) => !keys.includes(key)),
    );
    submit(next, message);
  }

  return { onSubmit, resetField };
}
