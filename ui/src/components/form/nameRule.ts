// The client-side twin of the server's one name rule (dc_core.names, quick 261120-lbz, D-01).
// The server stays authoritative; this only tells the user before they submit.
//
// Allowed: letters, marks and numbers in any script, the regular space and - _ . , ( ) & ' / : # + @.
// Refused: emoji, symbols, control and format characters (bidi, zero-width, BOM), private use,
// unassigned and surrogate code points, every space other than U+0020, tabs and newlines. Variation
// selectors and combining enclosing marks are marks but make emoji, so they are refused too.

export const NAME_RULE_MESSAGE =
  "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only.";

const PUNCTUATION = " -_.,()&'/:#+@";
const ALLOWED = /^[\p{L}\p{M}\p{N}]$/u;
const REFUSED_RANGES: readonly (readonly [number, number])[] = [
  [0xfe00, 0xfe0f],
  [0xe0100, 0xe01ef],
  [0x20dd, 0x20e4],
];

function allowed(char: string): boolean {
  if (PUNCTUATION.includes(char)) return true;
  const code = char.codePointAt(0) ?? 0;
  if (REFUSED_RANGES.some(([low, high]) => code >= low && code <= high))
    return false;
  return ALLOWED.test(char);
}

/** NAME_RULE_MESSAGE when `value` holds a refused character, else undefined (blank is the form's own required-field message). */
export function nameRuleError(value: string): string | undefined {
  for (const char of value) {
    if (!allowed(char)) return NAME_RULE_MESSAGE;
  }
  return undefined;
}
