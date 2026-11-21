// Server validation errors onto form fields (D-UI-08-24). A 422 `validation-error` carries
// `errors[{pointer, detail}]` with JSON Pointers into the request body; the form passes the
// pointers it knows and gets the per-field messages plus the rest for the ProblemAlert list.
import type { Problem, ProblemFieldError } from "./problem";

export type MappedFieldErrors = {
  /** Field name to message; the first error for a field wins, later ones go to `unmapped`. */
  fieldErrors: Record<string, string>;
  unmapped: ProblemFieldError[];
};

/** `fields` maps a JSON Pointer (for example "/new_password") to a form field name. */
export function mapFieldErrors(
  problem: Problem,
  fields: Record<string, string>,
): MappedFieldErrors {
  const fieldErrors: Record<string, string> = {};
  const unmapped: ProblemFieldError[] = [];
  if (problem.kind !== "problem") return { fieldErrors, unmapped };
  for (const error of problem.errors) {
    const field = Object.hasOwn(fields, error.pointer)
      ? fields[error.pointer]
      : undefined;
    if (field === undefined || Object.hasOwn(fieldErrors, field)) {
      // A second error for an already filled field stays visible in the alert list.
      unmapped.push(error);
    } else {
      fieldErrors[field] = error.detail;
    }
  }
  return { fieldErrors, unmapped };
}
