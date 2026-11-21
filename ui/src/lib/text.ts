// Linear-time string helpers. Regexes with a quantified class before an end anchor or next to
// another overlapping quantifier (trailing-run trims, trailing whitespace, an unanchored
// digits-dot-digits-dot-digits pattern) backtrack quadratically on hostile input, so
// trimming, slugs, the first x.y.z and trailing whitespace are done with index loops and string
// methods instead (SonarQube typescript:S8786).

/** `value` without leading and trailing runs of `char` (one character). */
export function trimChar(value: string, char: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && value[start] === char) start += 1;
  while (end > start && value[end - 1] === char) end -= 1;
  return value.slice(start, end);
}

/** `value` without a trailing run of `char` (one character). */
export function trimCharEnd(value: string, char: string): string {
  let end = value.length;
  while (end > 0 && value[end - 1] === char) end -= 1;
  return value.slice(0, end);
}

/**
 * Lowercase letters, digits and single hyphens: every run of other characters becomes one hyphen
 * and none stays at either end. With `maxLength` the result is cut to that length and a hyphen
 * the cut leaves at the end is dropped.
 */
export function toSlug(value: string, maxLength?: number): string {
  const slug = trimChar(value.toLowerCase().replace(/[^a-z0-9]+/g, "-"), "-");
  return maxLength === undefined
    ? slug
    : trimCharEnd(slug.slice(0, maxLength), "-");
}

function isAsciiDigit(code: number): boolean {
  return code >= 48 && code <= 57;
}

/** The index after the ASCII digit run that starts at `from` (`from` when there is none). */
function digitRunEnd(text: string, from: number): number {
  let end = from;
  while (end < text.length && isAsciiDigit(text.charCodeAt(end))) end += 1;
  return end;
}

/**
 * The leftmost `x.y.z` of ASCII digit runs, like `/(\d+\.\d+\.\d+)/.exec(text)?.[1]`: "v1.2.3.4"
 * gives "1.2.3", "1..2.3.4" gives "2.3.4", "12.3" gives undefined.
 */
export function firstSemver(text: string): string | undefined {
  let from = 0;
  while (from < text.length) {
    const first = digitRunEnd(text, from);
    if (first === from) {
      from += 1;
      continue;
    }
    // A match can only start at the beginning of a digit run, and each part is the whole run.
    let end = first;
    let parts = 1;
    while (parts < 3 && text[end] === ".") {
      const next = digitRunEnd(text, end + 1);
      if (next === end + 1) break;
      end = next;
      parts += 1;
    }
    if (parts === 3) return text.slice(from, end);
    from = first;
  }
  return undefined;
}

/** Trailing whitespace of `value` replaced by exactly one space; unchanged without any. */
export function collapseTrailingWhitespace(value: string): string {
  const trimmed = value.trimEnd();
  return trimmed.length === value.length ? value : `${trimmed} `;
}

/** The one text order of the UI: `localeCompare` with an explicit locale. */
export function compareText(a: string, b: string): number {
  return a.localeCompare(b, "en");
}
