// Settings > Platform (quick 261120-0yc: P1 banner, P3 About). The banner is plain text (1 to 500
// characters), info or warning, with an optional window; the preview draws it with the same
// component the shell uses. About lists every service's build and database state, view only; a
// service that cannot be asked reads "Unreachable" as a neutral badge, never an alert.
import { useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import type { Problem } from "@/api/problem";
import type { SettingsAboutService, SettingsSection } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { DataTable, orNotSet, type Column } from "@/components/DataTable";
import { Alert } from "@/components/Alert";
import { SegmentedControl } from "@/components/SegmentedControl";
import { TextField, TextareaField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { useUiBuild } from "@/buildInfo";
import { fromLocalInput, toLocalInput } from "@/features/audit/query";
import { formatDateTime } from "@/features/admin/shared";
import { AnnouncementView } from "@/features/shell/AnnouncementBanner";
import { problemOf } from "@/lib/unexpected";
import { useAbout, useSaveSection } from "./api";
import { SaveProblem } from "./SaveProblem";
import styles from "./settings.module.css";

const TEXT_MAX = 500;
type Severity = "info" | "warning";

type StoredBanner = {
  text: string;
  severity: Severity;
  starts_at?: string;
  ends_at?: string;
};

function storedBanner(values: Record<string, unknown>): StoredBanner | null {
  const raw = values.banner;
  if (typeof raw !== "object" || raw === null) return null;
  const banner = raw as Record<string, unknown>;
  if (typeof banner.text !== "string") return null;
  return {
    text: banner.text,
    severity: banner.severity === "warning" ? "warning" : "info",
    starts_at:
      typeof banner.starts_at === "string" ? banner.starts_at : undefined,
    ends_at: typeof banner.ends_at === "string" ? banner.ends_at : undefined,
  };
}

function BannerForm({
  section,
  onReload,
}: {
  section: SettingsSection;
  onReload: () => void;
}) {
  const save = useSaveSection();
  const stored = storedBanner(section.values);
  const [text, setText] = useState(stored?.text ?? "");
  const [severity, setSeverity] = useState<Severity>(
    stored?.severity ?? "info",
  );
  const [starts, setStarts] = useState(toLocalInput(stored?.starts_at));
  const [ends, setEnds] = useState(toLocalInput(stored?.ends_at));
  const [errors, setErrors] = useState<{ text?: string; ends?: string }>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  // A new etag (after a save or a reload) means new stored values: take them, keep the "saved"
  // note. State adjusted during render, so the form is not remounted and the note survives.
  const [seen, setSeen] = useState(section.etag);
  if (seen !== section.etag) {
    setSeen(section.etag);
    const next = storedBanner(section.values);
    setText(next?.text ?? "");
    setSeverity(next?.severity ?? "info");
    setStarts(toLocalInput(next?.starts_at));
    setEnds(toLocalInput(next?.ends_at));
    setProblem(null);
  }

  function submit(values: Record<string, unknown>, message: string) {
    setProblem(null);
    setErrors({});
    setSaved(null);
    // The section is replaced as a whole, so the retention setting rides along unchanged.
    const others = Object.fromEntries(
      Object.entries(section.values).filter(([key]) => key !== "banner"),
    );
    save.mutate(
      {
        section: section.section,
        values: { ...others, ...values },
        etag: section.etag,
      },
      {
        onSuccess: () => {
          setSaved(message);
        },
        onError: (failure) => {
          const found = problemOf(failure);
          const mapped = mapFieldErrors(found, {
            "/banner/text": "text",
            "/banner": "ends",
          });
          setErrors(mapped.fieldErrors);
          setProblem(
            mapped.unmapped.length > 0 ||
              Object.keys(mapped.fieldErrors).length === 0
              ? found
              : null,
          );
        },
      },
    );
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = text.trim();
    if (trimmed === "") {
      setErrors({ text: "Enter the announcement text, or clear the banner." });
      return;
    }
    const startsAt = fromLocalInput(starts);
    const endsAt = fromLocalInput(ends);
    if (startsAt !== undefined && endsAt !== undefined && endsAt <= startsAt) {
      setErrors({ ends: "The end must be after the start." });
      return;
    }
    submit(
      {
        banner: {
          text: trimmed,
          severity,
          ...(startsAt !== undefined ? { starts_at: startsAt } : {}),
          ...(endsAt !== undefined ? { ends_at: endsAt } : {}),
        },
      },
      "Banner saved.",
    );
  }

  function clear() {
    setText("");
    setSeverity("info");
    setStarts("");
    setEnds("");
    submit({}, "Banner cleared.");
  }

  const preview = text.trim() === "" ? null : { text: text.trim(), severity };
  return (
    <form className={styles.stack} noValidate onSubmit={onSubmit}>
      {problem ? (
        <SaveProblem
          problem={problem}
          onReload={onReload}
          fieldLabels={{ "/banner/text": "Banner text", "/banner": "Banner" }}
        />
      ) : null}
      {saved ? (
        <Alert intent="success" title="Saved">
          {saved}
        </Alert>
      ) : null}
      <div className={styles.form}>
        <TextareaField
          id="settings-banner-text"
          label="Banner text"
          helper="Plain text shown to every signed-in user. No formatting or line breaks."
          rows={3}
          maxLength={TEXT_MAX}
          value={text}
          error={errors.text}
          onChange={(event) => {
            setText(event.target.value.replace(/[\r\n]+/g, " "));
            setSaved(null);
          }}
        />
        <span className={styles.counter} aria-live="polite">
          {String(text.length)} / {String(TEXT_MAX)} characters
        </span>
        <SegmentedControl<Severity>
          label="Severity"
          value={severity}
          options={[
            { value: "info", label: "Info" },
            { value: "warning", label: "Warning" },
          ]}
          onChange={(value) => {
            setSeverity(value);
            setSaved(null);
          }}
        />
        <div className={styles.windowRow}>
          <TextField
            id="settings-banner-start"
            label="Starts"
            type="datetime-local"
            optional
            autoComplete="off"
            value={starts}
            onChange={(event) => {
              setStarts(event.target.value);
            }}
          />
          <TextField
            id="settings-banner-end"
            label="Ends"
            type="datetime-local"
            optional
            autoComplete="off"
            value={ends}
            error={errors.ends}
            onChange={(event) => {
              setEnds(event.target.value);
            }}
          />
        </div>
        <div className={styles.fieldMeta}>
          <span>Default: no banner</span>
        </div>
      </div>
      <div className={styles.preview}>
        <p className={styles.previewLabel}>Preview</p>
        {preview ? (
          <AnnouncementView banner={preview} />
        ) : (
          <span className={styles.counter}>
            The banner appears here as you type.
          </span>
        )}
      </div>
      <div className={styles.actions}>
        <Button
          type="submit"
          variant="primary"
          size="small"
          busy={save.isPending}
          busyLabel="Saving…"
        >
          Save banner
        </Button>
        <Button
          variant="ghost"
          size="small"
          disabled={stored === null}
          onClick={clear}
        >
          Reset to default
        </Button>
      </div>
    </form>
  );
}

const RETENTION_MIN = 90;
const RETENTION_MAX = 36500;
type RetentionMode = "forever" | "days";

function retentionDefault(section: SettingsSection): number | null {
  const value = section.defaults.audit_retention_days;
  return typeof value === "number" && value > 0 ? value : null;
}

function retentionInitial(section: SettingsSection): {
  mode: RetentionMode;
  days: string;
} {
  const stored = section.values.audit_retention_days;
  const value =
    typeof stored === "number" ? stored : (retentionDefault(section) ?? 0);
  return value > 0
    ? { mode: "days", days: String(value) }
    : { mode: "forever", days: "" };
}

function RetentionForm({
  section,
  onReload,
}: {
  section: SettingsSection;
  onReload: () => void;
}) {
  const save = useSaveSection();
  const initial = retentionInitial(section);
  const [mode, setMode] = useState<RetentionMode>(initial.mode);
  const [days, setDays] = useState(initial.days);
  const [error, setError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [seen, setSeen] = useState(section.etag);
  if (seen !== section.etag) {
    setSeen(section.etag);
    const next = retentionInitial(section);
    setMode(next.mode);
    setDays(next.days);
    setProblem(null);
  }
  const fallback = retentionDefault(section);

  function submit(retention: number | null, message: string) {
    // The section is replaced as a whole, so the banner rides along unchanged.
    const others = Object.fromEntries(
      Object.entries(section.values).filter(
        ([key]) => key !== "audit_retention_days",
      ),
    );
    setProblem(null);
    setError(undefined);
    setSaved(null);
    save.mutate(
      {
        section: section.section,
        values: {
          ...others,
          ...(retention === null ? {} : { audit_retention_days: retention }),
        },
        etag: section.etag,
      },
      {
        onSuccess: () => {
          setSaved(message);
        },
        onError: (failure) => {
          const found = problemOf(failure);
          const mapped = mapFieldErrors(found, {
            "/audit_retention_days": "days",
          });
          setError(mapped.fieldErrors.days);
          setProblem(
            mapped.unmapped.length > 0 ||
              Object.keys(mapped.fieldErrors).length === 0
              ? found
              : null,
          );
        },
      },
    );
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mode === "forever") {
      // The default keeps everything unless the environment sets a retention: then store 0.
      submit(fallback === null ? null : 0, "Audit retention saved.");
      return;
    }
    const n = /^\d{1,5}$/.test(days.trim()) ? Number(days.trim()) : 0;
    if (n < RETENTION_MIN || n > RETENTION_MAX) {
      setError(
        `Enter a whole number of days from ${String(RETENTION_MIN)} to ${String(RETENTION_MAX)}.`,
      );
      return;
    }
    submit(n === fallback ? null : n, "Audit retention saved.");
  }

  return (
    <form className={styles.stack} noValidate onSubmit={onSubmit}>
      {problem ? (
        <SaveProblem
          problem={problem}
          onReload={onReload}
          fieldLabels={{ "/audit_retention_days": "Audit retention" }}
        />
      ) : null}
      {saved ? (
        <Alert intent="success" title="Saved">
          {saved}
        </Alert>
      ) : null}
      <div className={styles.form}>
        <SegmentedControl<RetentionMode>
          label="Audit events"
          value={mode}
          options={[
            { value: "forever", label: "Keep forever" },
            { value: "days", label: "Delete after N days" },
          ]}
          onChange={(value) => {
            setMode(value);
            setSaved(null);
            setError(undefined);
          }}
        />
        {mode === "days" ? (
          <TextField
            id="settings-retention-days"
            label="Delete events older than (days)"
            helper={`At least ${String(RETENTION_MIN)} days. Settings changes are always kept.`}
            inputMode="numeric"
            autoComplete="off"
            value={days}
            error={error}
            onChange={(event) => {
              setDays(event.target.value);
              setSaved(null);
            }}
          />
        ) : (
          <span className={styles.counter}>
            Settings changes are always kept, whatever the retention.
          </span>
        )}
        <div className={styles.fieldMeta}>
          <span>
            Default:{" "}
            {fallback === null ? "keep forever" : `${String(fallback)} days`}
          </span>
        </div>
      </div>
      <div className={styles.actions}>
        <Button
          type="submit"
          variant="primary"
          size="small"
          busy={save.isPending}
          busyLabel="Saving…"
        >
          Save retention
        </Button>
        <Button
          variant="ghost"
          size="small"
          disabled={section.values.audit_retention_days === undefined}
          aria-label="Reset to default for audit retention"
          onClick={() => {
            submit(null, "Audit retention reset.");
          }}
        >
          Reset to default
        </Button>
      </div>
    </form>
  );
}

type AboutRow = {
  service: string;
  status: "ok" | "unreachable";
  version: string | null;
  commit: string | null;
  built_at: string | null;
  migration_head: string | null;
  database: string | null;
};

function aboutColumns(uiVersion: string): Column<AboutRow>[] {
  return [
    {
      id: "service",
      header: "Service",
      rowHeader: true,
      cell: (r) => r.service,
    },
    {
      id: "version",
      header: "Version",
      // One release gives every component the same version; another one is named in text.
      cell: (r) =>
        r.version !== null && r.version !== uiVersion ? (
          <span className={styles.skew}>
            {r.version}
            <Badge intent="warning">Different version</Badge>
          </span>
        ) : (
          orNotSet(r.version)
        ),
    },
    {
      id: "commit",
      header: "Commit",
      cell: (r) => <span className={styles.ident}>{r.commit ?? "—"}</span>,
    },
    {
      id: "built",
      header: "Built",
      cell: (r) =>
        r.built_at === null || r.built_at === "dev"
          ? (r.built_at ?? "—")
          : formatDateTime(r.built_at),
    },
    {
      id: "head",
      header: "Migration head",
      cell: (r) => (
        <span className={styles.ident}>{r.migration_head ?? "—"}</span>
      ),
    },
    {
      id: "database",
      header: "Database",
      cell: (r) => r.database ?? "—",
    },
    {
      id: "status",
      header: "Status",
      nowrap: true,
      cell: (r) =>
        r.status === "ok" ? (
          <Badge intent="success">OK</Badge>
        ) : (
          <Badge intent="neutral">Unreachable</Badge>
        ),
    },
  ];
}

function About() {
  const about = useAbout();
  const uiBuild = useUiBuild();
  if (about.isError) {
    return (
      <ProblemAlert
        problem={problemOf(about.error)}
        onRetry={() => void about.refetch()}
      />
    );
  }
  if (about.data === undefined) return <Skeleton label="About" rows={5} />;
  const ui: AboutRow = {
    service: "ui",
    status: "ok",
    version: uiBuild.version,
    commit: uiBuild.commit,
    built_at: uiBuild.builtAt,
    migration_head: null,
    database: "none",
  };
  const rows: AboutRow[] = [
    ui,
    ...about.data.services.map((s: SettingsAboutService) => ({ ...s })),
  ];
  return (
    <>
      <p className={styles.platformVersion}>Contractum {uiBuild.version}</p>
      <DataTable
        caption="Services"
        captionHidden
        columns={aboutColumns(uiBuild.version)}
        rows={rows}
        getRowId={(row) => row.service}
      />
    </>
  );
}

export function PlatformTab({
  section,
  onReload,
}: {
  section: SettingsSection;
  choices?: unknown;
  onReload: () => void;
}) {
  return (
    <div className={styles.stack}>
      <Card title="Announcement banner">
        <BannerForm section={section} onReload={onReload} />
      </Card>
      <Card title="Audit retention">
        <RetentionForm section={section} onReload={onReload} />
      </Card>
      <Card title="About">
        <About />
      </Card>
    </div>
  );
}
