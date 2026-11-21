// Audit explorer search params (D-UI-13-05): one typed, bookmarkable object that maps 1:1 to the
// API's query parameters, plus `event` (the open detail drawer). Times are stored as RFC 3339 UTC
// and shown in local `datetime-local` inputs; the range is half-open, [from, to).

export const CATEGORIES = ["change", "access", "denial"] as const;
export type AuditCategory = (typeof CATEGORIES)[number];

export type AuditSearch = {
  aggregate_type?: string;
  aggregate_id?: string;
  actor_id?: string;
  service?: string;
  action?: string;
  category?: AuditCategory;
  from?: string;
  to?: string;
  /** The event shown in the detail drawer. */
  event?: string;
};

/** Filter keys, in display order (everything except `event`). */
export const FILTER_KEYS = [
  "aggregate_type",
  "aggregate_id",
  "actor_id",
  "service",
  "action",
  "category",
  "from",
  "to",
] as const;
export type AuditFilters = Omit<AuditSearch, "event">;

/** Aggregate types seen in the event catalogue; the field also takes free text. */
export const AGGREGATE_TYPES = [
  "api_credential",
  "authz",
  "card",
  "contract",
  "entra_registration",
  "entra_user",
  "folder",
  "grant",
  "group",
  "rule_set",
  "service_account",
  "system",
  "template",
  "user",
];

export const SERVICES = ["identity", "contracts", "validation", "audit"];

function text(value: unknown, max = 200): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed.slice(0, max);
}

function instant(value: unknown): string | undefined {
  const found = text(value, 40);
  if (found === undefined) return undefined;
  return Number.isNaN(Date.parse(found)) ? undefined : found;
}

/** The route's `validateSearch`: unknown or malformed values are dropped, never trusted. */
export function validateAuditSearch(
  search: Record<string, unknown>,
): AuditSearch {
  const result: AuditSearch = {};
  const put = <K extends keyof AuditSearch>(key: K, value: AuditSearch[K]) => {
    if (value !== undefined) result[key] = value;
  };
  put("aggregate_type", text(search.aggregate_type, 64));
  put("aggregate_id", text(search.aggregate_id));
  put("actor_id", text(search.actor_id));
  put("service", text(search.service, 64));
  put("action", text(search.action, 128));
  put(
    "category",
    CATEGORIES.includes(search.category as AuditCategory)
      ? (search.category as AuditCategory)
      : undefined,
  );
  put("from", instant(search.from));
  put("to", instant(search.to));
  put("event", text(search.event, 64));
  return result;
}

/** The API query for one page: only set filters, times normalised to RFC 3339 UTC. */
export function toApiQuery(filters: AuditFilters): Record<string, string> {
  const query: Record<string, string> = {};
  for (const key of FILTER_KEYS) {
    const value = filters[key];
    if (value === undefined || value === "") continue;
    query[key] = key === "from" || key === "to" ? toUtc(value) : value;
  }
  return query;
}

/** Any parseable instant as RFC 3339 UTC with a `Z` ("2026-11-15T09:30:00.000Z"). */
export function toUtc(value: string): string {
  const time = Date.parse(value);
  return Number.isNaN(time) ? value : new Date(time).toISOString();
}

const pad = (n: number) => String(n).padStart(2, "0");

/** RFC 3339 → the local wall-clock value of a `datetime-local` input ("2026-11-15T10:30"). */
export function toLocalInput(value: string | undefined): string {
  if (value === undefined) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** A `datetime-local` value (local wall clock) → RFC 3339 UTC, or undefined when empty. */
export function fromLocalInput(value: string): string | undefined {
  if (value === "") return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

export const PRESETS = [
  { id: "1h", label: "Last hour", ms: 3_600_000 },
  { id: "24h", label: "Last 24 hours", ms: 86_400_000 },
  { id: "7d", label: "Last 7 days", ms: 7 * 86_400_000 },
  { id: "30d", label: "Last 30 days", ms: 30 * 86_400_000 },
] as const;

/** A preset range ending now: `from` only, so new events keep appearing. */
export function presetRange(
  ms: number,
  now = Date.now(),
): Pick<AuditFilters, "from" | "to"> {
  return { from: new Date(now - ms).toISOString(), to: undefined };
}

export function hasFilters(filters: AuditFilters): boolean {
  return FILTER_KEYS.some((key) => filters[key] !== undefined);
}
