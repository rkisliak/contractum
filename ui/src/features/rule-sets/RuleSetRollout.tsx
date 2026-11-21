// The Rollout tab (D-UI-11-11/12): the mandatory switch (owner sets only, ruleset:mandate) and
// the schedule of every published version with the server's mode and enforce date. "Enforce
// now" moves a warning version's date to now; platform sets go through the ROOT folder route.
// Quick 261118-vpa adds Pause/Resume for platform sets (ROOT ruleset:mandate: the set keeps
// running and reporting but never blocks) and "Change date..." for any resolved version (any
// date, either way). The mode shown is always the server's value; the UI never derives it from
// the clock.
import { useState } from "react";
import type { ResolvedEntry, RuleSetVersionSummary } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { Checkbox, TextField } from "@/components/form";
import { EmptyState } from "@/components/states";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { useRuleSetWrites } from "./api";
import { formatUtcDate, relativeDays } from "./rollout";
import styles from "@/features/governance/gov.module.css";

type Row = RuleSetVersionSummary & { resolved: ResolvedEntry | undefined };

export function RuleSetRollout({
  ruleSetId,
  name,
  folderLabel,
  mandatory,
  versions,
  resolved,
  paused = false,
  canMandate,
  canEnforce,
  canPause = false,
  canReschedule = false,
  platformRootId,
}: {
  ruleSetId: string;
  name: string;
  folderLabel: string;
  mandatory: boolean;
  versions: RuleSetVersionSummary[];
  resolved: ResolvedEntry[];
  /** A paused platform set: warn only, its rules still run and report. */
  paused?: boolean;
  /** Owner set and ruleset:mandate at its folder (never for platform sets). */
  canMandate: boolean;
  canEnforce: boolean;
  /** Platform set and ruleset:mandate at ROOT: Pause / Resume. */
  canPause?: boolean;
  /** ruleset:mandate where the set is held (ROOT for platform sets): Change date... */
  canReschedule?: boolean;
  /** For a platform set: the ROOT folder id its enforce-now route goes through. */
  platformRootId?: string;
}) {
  const writes = useRuleSetWrites(ruleSetId);
  const [confirm, setConfirm] = useState<"on" | "off" | null>(null);
  const [enforcing, setEnforcing] = useState<number | null>(null);
  const [pauseDialog, setPauseDialog] = useState(false);
  const [rescheduling, setRescheduling] = useState<number | null>(null);
  const [date, setDate] = useState("");
  const [dateError, setDateError] = useState<string | undefined>(undefined);
  const byVersion = new Map(resolved.map((entry) => [entry.version_no, entry]));
  const rows: Row[] = [...versions]
    .sort((a, b) => b.version_no - a.version_no)
    .map((version) => ({
      ...version,
      resolved: byVersion.get(version.version_no),
    }));

  const columns: Column<Row>[] = [
    {
      id: "version",
      header: "Version",
      rowHeader: true,
      cell: (row) => `Version ${String(row.version_no)}`,
    },
    {
      id: "mode",
      header: "Mode",
      cell: (row) =>
        row.resolved === undefined ? (
          <Badge intent="neutral">
            {row.deprecated_at !== null ? "Deprecated" : "Not applied"}
          </Badge>
        ) : row.resolved.paused ? (
          <Badge intent="warning">Paused</Badge>
        ) : row.resolved.mode === "enforce" ? (
          <Badge intent="danger">Enforced</Badge>
        ) : (
          <Badge intent="warning">Warning</Badge>
        ),
    },
    {
      id: "enforce",
      header: "Blocks publishes from",
      cell: (row) => {
        const at = row.resolved?.enforce_at ?? row.enforce_at;
        if (at === null) return "Not scheduled";
        if (row.resolved?.paused)
          return `${formatUtcDate(at)} UTC once resumed`;
        return `${formatUtcDate(at)} UTC (${relativeDays(at)})`;
      },
    },
    {
      id: "actions",
      header: "Actions",
      cell: (row) => {
        const entry = row.resolved;
        const enforce = canEnforce && entry?.mode === "warn" && !entry.paused;
        const change = canReschedule && entry !== undefined;
        if (!enforce && !change) return null;
        return (
          <span className={styles.actions}>
            {enforce ? (
              <Button
                variant="secondary"
                size="small"
                onClick={() => {
                  setEnforcing(row.version_no);
                }}
              >
                Enforce version {row.version_no} now
              </Button>
            ) : null}
            {change ? (
              <Button
                variant="secondary"
                size="small"
                onClick={() => {
                  const at = entry.enforce_at ?? row.enforce_at;
                  setDate(at === null ? "" : at.slice(0, 10));
                  setDateError(undefined);
                  setRescheduling(row.version_no);
                }}
              >
                Change date…{" "}
                <VisuallyHidden>version {row.version_no}</VisuallyHidden>
              </Button>
            ) : null}
          </span>
        );
      },
    },
  ];

  const pauseCopy = paused
    ? {
        title: `Resume ${name}?`,
        body: "An enforced version blocks publishes again at once; a version still in its warning period keeps warning until its date.",
        confirm: `Resume ${name}`,
        busy: "Resuming…",
      }
    : {
        title: `Pause ${name}?`,
        body: "Its rules keep running and report findings on every save, validation and publish, but they won't block publishing until it is resumed. This applies to every folder.",
        confirm: `Pause ${name}`,
        busy: "Pausing…",
      };

  return (
    <div className={styles.stack}>
      {canPause ? (
        <div className={styles.actions}>
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              setPauseDialog(true);
            }}
          >
            {paused ? "Resume" : "Pause"}
          </Button>
        </div>
      ) : null}
      {canMandate ? (
        <Checkbox
          id="rule-set-mandatory"
          label={`Mandatory for ${folderLabel} and everything below`}
          helper="A mandatory rule set applies to every contract below its folder. Its newest version warns for a grace period, then blocks publishes."
          checked={mandatory}
          onChange={(event) => {
            setConfirm(event.target.checked ? "on" : "off");
          }}
        />
      ) : (
        <p className={styles.muted}>
          {mandatory
            ? `Mandatory for ${folderLabel} and everything below.`
            : "Not mandatory: it applies only where a contract attaches it."}
        </p>
      )}
      {rows.length === 0 ? (
        <EmptyState
          level={3}
          heading="Nothing published yet"
          body="The schedule appears once a version is published."
        />
      ) : (
        <DataTable
          caption={`Rollout schedule of ${name}`}
          captionHidden
          columns={columns}
          rows={rows}
          getRowId={(row) => String(row.version_no)}
        />
      )}
      <ConfirmDialog
        open={confirm === "on"}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
        title={`Make ${name} mandatory?`}
        body={`It applies to ${folderLabel} and everything below. Its newest version warns for the grace period, then blocks publishes. No lower level can override it.`}
        dismissLabel="Go back"
        confirmLabel="Make mandatory"
        busyLabel="Saving…"
        onConfirm={async () => {
          await writes.setMandatory.mutateAsync(true);
        }}
      />
      <ConfirmDialog
        open={confirm === "off"}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
        title={`Stop requiring ${name}?`}
        body="Lower levels keep any attachments they made."
        dismissLabel="Go back"
        confirmLabel="Stop requiring"
        busyLabel="Saving…"
        onConfirm={async () => {
          await writes.setMandatory.mutateAsync(false);
        }}
      />
      <ConfirmDialog
        open={enforcing !== null}
        onOpenChange={(open) => {
          if (!open) setEnforcing(null);
        }}
        title={`Enforce version ${String(enforcing ?? "")} now?`}
        body={`Publishes that break its rules are blocked immediately, everywhere below ${folderLabel}.`}
        dismissLabel="Go back"
        confirmLabel="Enforce now"
        busyLabel="Enforcing…"
        onConfirm={async () => {
          if (enforcing === null) return;
          await writes.enforceNow.mutateAsync({
            versionNo: enforcing,
            ...(platformRootId ? { platformRootId } : {}),
          });
        }}
      />
      <ConfirmDialog
        open={pauseDialog}
        onOpenChange={setPauseDialog}
        title={pauseCopy.title}
        body={pauseCopy.body}
        dismissLabel="Go back"
        confirmLabel={pauseCopy.confirm}
        busyLabel={pauseCopy.busy}
        onConfirm={async () => {
          if (platformRootId === undefined) return;
          await writes.setPaused.mutateAsync({
            platformRootId,
            paused: !paused,
          });
        }}
      />
      <ConfirmDialog
        open={rescheduling !== null}
        onOpenChange={(open) => {
          if (!open) setRescheduling(null);
        }}
        title={`Change the enforcement date of version ${String(rescheduling ?? "")}?`}
        body={
          <>
            <p>Publishes that break its rules are blocked from this date.</p>
            <TextField
              id="reschedule-date"
              autoComplete="off"
              label="Blocks publishes from (UTC)"
              helper="Any date. Today or an earlier date enforces it at once; a later date warns until then."
              type="date"
              aria-required="true"
              value={date}
              error={dateError}
              onChange={(event) => {
                setDate(event.target.value);
                setDateError(undefined);
              }}
            />
          </>
        }
        dismissLabel="Go back"
        confirmLabel="Save date"
        busyLabel="Saving…"
        onConfirm={async () => {
          if (rescheduling === null) return;
          if (date === "") {
            setDateError("Choose a date.");
            throw new Error("missing date");
          }
          await writes.reschedule.mutateAsync({
            versionNo: rescheduling,
            enforceAt: `${date}T00:00:00Z`,
            ...(platformRootId ? { platformRootId } : {}),
          });
        }}
      />
    </div>
  );
}
