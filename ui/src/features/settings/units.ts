// Units of the security settings (quick 261120-0ye). The API stores seconds and days; the form
// shows hours, minutes and days. One module converts both ways, so no screen does its own sums.
export type Unit = "count" | "days" | "hours" | "minutes";

const SECONDS: Record<Unit, number> = {
  count: 1,
  days: 1,
  hours: 3600,
  minutes: 60,
};

/** The stored number as the text of its input (at most two decimals, no trailing zeros). */
export function toDisplay(value: number, unit: Unit): string {
  return String(Number((value / SECONDS[unit]).toFixed(2)));
}

/**
 * The stored number for an input's text, or null when it is not a plain non-negative number
 * (digits with at most two decimals; counts and days must be whole).
 */
export function fromDisplay(text: string, unit: Unit): number | null {
  const trimmed = text.trim();
  const whole = unit === "count" || unit === "days";
  const pattern = whole ? /^\d{1,7}$/ : /^\d{1,7}(\.\d{1,2})?$/;
  if (!pattern.test(trimmed)) return null;
  return Math.round(Number(trimmed) * SECONDS[unit]);
}

/** "Between 1 and 20" style hint for a numeric field. */
export function rangeHint(min: number, max: number, unit: Unit): string {
  const noun =
    unit === "count"
      ? ""
      : unit === "days"
        ? " days"
        : unit === "hours"
          ? " hours"
          : " minutes";
  return `${toDisplay(min, unit)} to ${toDisplay(max, unit)}${noun}`;
}

/** Hours and minutes for a duration in seconds: 90000 becomes "25 h", 900 "15 min". */
export function humanDuration(seconds: number): string {
  if (seconds % 86400 === 0)
    return `${String(seconds / 86400)} ${seconds === 86400 ? "day" : "days"}`;
  if (seconds % 3600 === 0) return `${String(seconds / 3600)} h`;
  if (seconds % 60 === 0) return `${String(seconds / 60)} min`;
  return `${String(seconds)} s`;
}
