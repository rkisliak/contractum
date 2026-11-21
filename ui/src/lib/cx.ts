// Joins CSS Module class names. Module lookups are typed `string | undefined` under
// noUncheckedIndexedAccess, so falsy parts are dropped rather than printed.
export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}
