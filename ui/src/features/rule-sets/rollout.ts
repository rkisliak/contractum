// Rollout display (D-UI-11-11): the mode of a mandatory version is computed by the server
// (`resolved[].mode`); the UI only formats it and never derives the mode from the clock.
import type { ResolvedEntry, RuleSetVersionSummary } from "@/api/types";

const dateFormat = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium",
  timeZone: "UTC",
});

/** "28 Nov 2026" in UTC, the date a warning version starts blocking. */
export function formatUtcDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : dateFormat.format(date);
}

/** "in 9 days", "today", "3 days ago": relative wording for an absolute date. */
export function relativeDays(value: string, now: Date = new Date()): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const days = Math.round((date.getTime() - now.getTime()) / 86_400_000);
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days === -1) return "yesterday";
  return days > 0 ? `in ${String(days)} days` : `${String(-days)} days ago`;
}

export type RolloutStatus = {
  label: string;
  intent: "danger" | "warning" | "neutral";
};

/**
 * The header status of a rule set: the newest resolved version's server mode. "Enforced",
 * "Warn until 28 Nov 2026", "Paused" (a platform set a Global admin paused: warn only) or "Not
 * mandatory". An absent `paused` reads as false.
 */
export function rolloutStatus(
  mandatory: boolean,
  resolved: ResolvedEntry[] | undefined,
  paused = false,
): RolloutStatus {
  if (!mandatory) return { label: "Not mandatory", intent: "neutral" };
  if (paused) return { label: "Paused", intent: "warning" };
  const newest = [...(resolved ?? [])].sort(
    (a, b) => b.version_no - a.version_no,
  )[0];
  if (newest === undefined)
    return { label: "Mandatory, nothing published", intent: "neutral" };
  if (newest.mode === "enforce") return { label: "Enforced", intent: "danger" };
  return {
    label:
      newest.enforce_at === null
        ? "Warning"
        : `Warn until ${formatUtcDate(newest.enforce_at)}`,
    intent: "warning",
  };
}

/**
 * The explanation line under a platform set's header: why a paused set does not block, or why a
 * newly added platform version is only warning for now. Null for an enforced or non-platform set.
 */
export function platformNote(input: {
  isPlatform: boolean;
  resolved: ResolvedEntry[] | undefined;
  versions: RuleSetVersionSummary[];
  paused?: boolean | undefined;
  pausedAt?: string | null | undefined;
}): string | null {
  if (!input.isPlatform) return null;
  if (input.paused === true) {
    const when =
      input.pausedAt === null || input.pausedAt === undefined
        ? ""
        : ` on ${formatUtcDate(input.pausedAt)}`;
    return `Paused${when}: its rules keep running and report findings, but they do not block publishing until resumed.`;
  }
  const newest = [...(input.resolved ?? [])].sort(
    (a, b) => b.version_no - a.version_no,
  )[0];
  if (newest === undefined || newest.mode !== "warn") return null;
  const published = input.versions.find(
    (version) => version.version_no === newest.version_no,
  )?.published_at;
  if (published === undefined || newest.enforce_at === null) return null;
  return `Added by a platform update on ${formatUtcDate(published)}; warns until ${formatUtcDate(newest.enforce_at)} so existing contracts can be fixed, then blocks publishing.`;
}

export function scopeLabel(scope: string, folderKind: string | null): string {
  if (scope === "platform") return "Platform";
  switch (folderKind) {
    case "organization":
      return "Organization";
    case "project":
      return "Project";
    case "team":
      return "Team";
    case "root":
      return "Root";
    default:
      return scope;
  }
}
