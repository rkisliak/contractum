// The validation report (docs/user/api-guide.md "Validation reports") is an untyped object in
// OpenAPI, so it is read through guards here and flattened into one finding list for the
// findings panel: structural errors first, then rule and native findings. Grouping follows the
// server's `blocking` flag; the UI never decides on its own what blocks (D-UI-10-07).
export type FindingGroup = "blocking" | "warning" | "info";

export type UnifiedFinding = {
  id: string;
  group: FindingGroup;
  /** Text label for the severity column. */
  severity: string;
  /** "ODCS schema", "Platform check" or the rule set name and version. */
  source: string;
  ruleKey: string | null;
  message: string;
  pointer: string | null;
  mode: string | null;
  /** The finding's rule set is a paused platform set: reported, never blocking. */
  paused: boolean;
};

export type ReportSummary = {
  publishable: boolean | null;
  blockingCount: number;
  status: string | null;
};

export type CompatibilityChange = {
  code: string;
  class: string;
  path: string;
  attribute: string | null;
  before: unknown;
  after: unknown;
  pointer_before: string | null;
  pointer_after: string | null;
  element_kind?: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function summaryOf(report: unknown): ReportSummary {
  if (!isRecord(report))
    return { publishable: null, blockingCount: 0, status: null };
  const summary = isRecord(report.summary) ? report.summary : {};
  return {
    publishable:
      typeof summary.publishable === "boolean" ? summary.publishable : null,
    blockingCount:
      typeof summary.blocking_count === "number" ? summary.blocking_count : 0,
    status: str(report.status),
  };
}

function severityLabel(severity: string | null, outcome: string | null) {
  if (outcome === "error") return "Rule error";
  if (severity === "error") return "Error";
  if (severity === "warn" || severity === "warning") return "Warning";
  if (severity === "info") return "Info";
  return severity ?? "Finding";
}

export function flattenReport(report: unknown): UnifiedFinding[] {
  if (!isRecord(report)) return [];
  const out: UnifiedFinding[] = [];
  const structural = isRecord(report.structural) ? report.structural : null;
  const structuralErrors = Array.isArray(structural?.errors)
    ? structural.errors
    : [];
  structuralErrors.forEach((entry, index) => {
    if (!isRecord(entry)) return;
    out.push({
      id: `s-${String(index)}`,
      group: "blocking",
      severity: "Error",
      source: "ODCS schema",
      ruleKey: str(entry.keyword),
      message:
        str(entry.message) ?? "The document does not match the ODCS schema.",
      pointer: str(entry.pointer),
      mode: null,
      paused: false,
    });
  });
  const findings = Array.isArray(report.findings) ? report.findings : [];
  findings.forEach((entry, index) => {
    if (!isRecord(entry)) return;
    const ruleSet = isRecord(entry.rule_set) ? entry.rule_set : {};
    const severity = str(entry.severity);
    const outcome = str(entry.outcome);
    const blocking = entry.blocking === true;
    const name = str(ruleSet.name);
    const version =
      typeof ruleSet.version === "number" ? ` v${String(ruleSet.version)}` : "";
    const location = isRecord(entry.location) ? entry.location : {};
    out.push({
      id: `f-${String(index)}`,
      group: blocking ? "blocking" : severity === "info" ? "info" : "warning",
      severity: severityLabel(severity, outcome),
      source:
        str(entry.source) === "native"
          ? "Platform check"
          : `${name ?? "Rule set"}${version}`,
      ruleKey: str(entry.rule_key),
      message: str(entry.message) ?? "",
      pointer: str(location.pointer),
      mode: str(entry.mode),
      paused: entry.paused === true,
    });
  });
  return out;
}

/** Structural errors from a create or save response (`structural_validation`). */
export function structuralOnly(structural: unknown): UnifiedFinding[] {
  return flattenReport({ structural });
}

export function compatibilityOf(report: unknown): {
  requiredBump: string | null;
  declared: string | null;
  changes: CompatibilityChange[];
} {
  const compat =
    isRecord(report) && isRecord(report.compatibility)
      ? report.compatibility
      : {};
  return {
    requiredBump: str(compat.required_bump),
    declared: str(compat.declared_version),
    changes: Array.isArray(compat.changes)
      ? (compat.changes.filter(isRecord) as CompatibilityChange[])
      : [],
  };
}

export function hasFinding(
  report: unknown,
  ruleKey: string,
): UnifiedFinding | undefined {
  return flattenReport(report).find((finding) => finding.ruleKey === ruleKey);
}

export const GROUP_LABEL: Record<FindingGroup, string> = {
  blocking: "Blocking",
  warning: "Warnings",
  info: "Info",
};

export function asRecord(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}
