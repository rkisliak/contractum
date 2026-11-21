// The return URL after sign-in (UI-SPEC 7.1, research Pattern 7, T-8-26): only a same-origin path
// that starts with a single "/" is followed. "//host", "/\host" and anything with a scheme fall
// back to "/".
const SINGLE_SLASH_PATH = /^\/(?![/\\])/;
// Control characters: a tab or newline can turn "/\t/host" into "//host" in a URL parser.
// eslint-disable-next-line no-control-regex
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;
const PROBE_ORIGIN = "https://redirect.invalid";

export function safeRedirect(value: string | undefined | null): string {
  if (typeof value !== "string") return "/";
  if (!SINGLE_SLASH_PATH.test(value) || CONTROL_CHARACTERS.test(value)) {
    return "/";
  }
  // Belt and braces: whatever the pattern let through must still resolve on this origin.
  try {
    if (new URL(value, PROBE_ORIGIN).origin !== PROBE_ORIGIN) return "/";
  } catch {
    return "/";
  }
  return value;
}
