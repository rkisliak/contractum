// The notices the sign-in page can show above its form (UI-SPEC 7.1). One definition: the
// /signin search schema validates against it and every caller that navigates there imports from
// here, so a notice can neither be misspelled nor smuggled in through the URL.

export const NOTICES = [
  "password-changed",
  "session-expired",
  "signed-out-elsewhere",
  "rotation-restart",
] as const;

export type Notice = (typeof NOTICES)[number];

export function isNotice(value: unknown): value is Notice {
  return (
    typeof value === "string" && (NOTICES as readonly string[]).includes(value)
  );
}
