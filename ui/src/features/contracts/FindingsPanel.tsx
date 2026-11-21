// The findings panel (D-UI-09-14, D-UI-10-05): severity as a text label plus an icon, the
// source (ODCS schema, platform check, or rule set and key), the message and the JSON pointer.
// Findings are grouped Blocking, Warnings, Info. A row whose pointer maps into the buffer has a
// "Go to line N" button that moves the editor cursor there.
import { AlertOctagon, AlertTriangle, Info } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";
import { Alert } from "@/components/Alert";
import { Badge, type BadgeIntent } from "@/components/Badge";
import { Button } from "@/components/Button";
import type { TextRange } from "@/features/editor/pointerMap";
import { GROUP_LABEL, type FindingGroup, type UnifiedFinding } from "./report";
import styles from "./Contracts.module.css";

const GROUP_ICON: Record<FindingGroup, LucideIcon> = {
  blocking: AlertOctagon,
  warning: AlertTriangle,
  info: Info,
};

const GROUP_INTENT: Record<FindingGroup, BadgeIntent> = {
  blocking: "danger",
  warning: "warning",
  info: "neutral",
};

// Findings computed for text that has since changed are not the current text's findings: they get
// their own wording, so nothing is labelled "Blocking" for a buffer it was not computed for.
const STALE_GROUP_LABEL: Record<FindingGroup, string> = {
  blocking: "Earlier errors",
  warning: "Earlier warnings",
  info: "Earlier info",
};

export type StaleFindings = {
  /** The revision the findings were computed for. */
  revision: number;
  /** Runs the page's save-and-validate (or validate) handler; the action is omitted without it. */
  onRevalidate?: () => void;
  revalidateLabel?: string;
  busy?: boolean;
};

export function FindingsPanel({
  findings,
  locate,
  onReveal,
  heading = "Findings",
  stale,
}: {
  findings: UnifiedFinding[];
  locate?: (pointer: string) => TextRange | null;
  onReveal?: (range: TextRange) => void;
  heading?: string;
  /** Set when the buffer changed after these results were computed (quick 261119-wk8). */
  stale?: StaleFindings;
}) {
  const groups: FindingGroup[] = ["blocking", "warning", "info"];
  const [showPrevious, setShowPrevious] = useState(false);
  const collapsed = stale !== undefined && !showPrevious;
  return (
    <section className={styles.findings} aria-label={heading}>
      <h2 className={styles.sectionHeading}>{heading}</h2>
      {stale ? (
        <>
          <Alert
            intent="warning"
            action={
              stale.onRevalidate ? (
                <Button
                  variant="secondary"
                  size="small"
                  busy={stale.busy}
                  busyLabel="Validating…"
                  onClick={stale.onRevalidate}
                >
                  {stale.revalidateLabel ?? "Save and validate"}
                </Button>
              ) : undefined
            }
          >
            These findings are for revision {String(stale.revision)}. Save and
            validate to check the current text.
          </Alert>
          <div>
            <Button
              variant="ghost"
              size="small"
              aria-expanded={showPrevious}
              onClick={() => {
                setShowPrevious((value) => !value);
              }}
            >
              {showPrevious
                ? "Hide previous findings"
                : "Show previous findings"}
            </Button>
          </div>
        </>
      ) : null}
      {collapsed ? null : findings.length === 0 ? (
        <p className={styles.muted}>
          {stale
            ? "The earlier results had no findings."
            : "No findings. Every check passed."}
        </p>
      ) : (
        groups.map((group) => {
          const rows = findings.filter((finding) => finding.group === group);
          if (rows.length === 0) return null;
          return (
            <div key={group} className={stale ? styles.staleGroup : undefined}>
              <h3 className={styles.groupHeading}>
                {stale ? STALE_GROUP_LABEL[group] : GROUP_LABEL[group]} (
                {rows.length})
              </h3>
              <div className={styles.tableScroll}>
                <table className={styles.findingTable}>
                  <thead>
                    <tr>
                      <th scope="col">Severity</th>
                      <th scope="col">Source</th>
                      <th scope="col">Message</th>
                      <th scope="col">Location</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((finding) => {
                      const range =
                        finding.pointer !== null && locate
                          ? locate(finding.pointer)
                          : null;
                      return (
                        <tr key={finding.id}>
                          <td>
                            <Badge
                              intent={GROUP_INTENT[group]}
                              icon={GROUP_ICON[group]}
                            >
                              {finding.severity}
                            </Badge>
                          </td>
                          <td>
                            {finding.source}
                            {finding.ruleKey ? (
                              <>
                                <br />
                                <code className={styles.mono}>
                                  {finding.ruleKey}
                                </code>
                              </>
                            ) : null}
                            {finding.mode === "warn" ? (
                              <>
                                <br />
                                <span className={styles.muted}>
                                  {finding.paused
                                    ? "paused, does not block"
                                    : "warn mode"}
                                </span>
                              </>
                            ) : null}
                          </td>
                          <td>
                            {range?.atParent ? "(at parent) " : ""}
                            {finding.message}
                          </td>
                          <td>
                            <code className={styles.mono}>
                              {finding.pointer === null
                                ? "location unavailable"
                                : finding.pointer === ""
                                  ? "/ (document)"
                                  : finding.pointer}
                            </code>
                            {range && onReveal ? (
                              <div>
                                <Button
                                  variant="ghost"
                                  size="small"
                                  onClick={() => {
                                    onReveal(range);
                                  }}
                                >
                                  Go to line {range.line}
                                </Button>
                              </div>
                            ) : finding.pointer !== null && locate && !range ? (
                              <div className={styles.muted}>
                                location unavailable
                              </div>
                            ) : null}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })
      )}
    </section>
  );
}

export function markersFor(
  findings: UnifiedFinding[],
  locate: (pointer: string) => TextRange | null,
) {
  return findings.flatMap((finding) => {
    if (finding.pointer === null) return [];
    const range = locate(finding.pointer);
    if (!range) return [];
    return [
      {
        line: range.line,
        column: range.column,
        endLine: range.endLine,
        endColumn: range.endColumn,
        severity:
          finding.group === "blocking"
            ? ("error" as const)
            : finding.group === "warning"
              ? ("warning" as const)
              : ("info" as const),
        message: `${finding.source}${finding.ruleKey ? ` · ${finding.ruleKey}` : ""}: ${finding.message}`,
      },
    ];
  });
}
