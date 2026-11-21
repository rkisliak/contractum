// Settings > Governance (quick 261120-0ye: G1 to G3). The grace period before a new version of a
// mandatory rule set is enforced, the default ODCS version of a new contract and whether
// unpublishing or deprecating a version needs a reason. Saving sends the whole section with
// `If-Match` (the 0yc pattern); a field equal to its default is simply not stored, and each
// field has "Default: X" and "Reset to default".
import { useState } from "react";
import type { Problem } from "@/api/problem";
import type { SettingsOut, SettingsSection } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { Checkbox, SelectField, TextField } from "@/components/form";
import { problemOf } from "@/lib/unexpected";
import { useSaveSection } from "./api";
import { SaveProblem } from "./SaveProblem";
import { sectionHandlers, type Built } from "./sectionHandlers";
import styles from "./settings.module.css";

const BUILTIN_VERSION = "v3.2.0";
const WARN_MAX = 365;

type Form = { warnDays: string; version: string; reason: boolean };
type FieldName = "warnDays" | "version";

const defaultWarn = (section: SettingsSection): number | null =>
  typeof section.defaults.mandatory_warn_days === "number"
    ? section.defaults.mandatory_warn_days
    : null;
const defaultVersion = (section: SettingsSection): string | null =>
  typeof section.defaults.default_odcs_api_version === "string"
    ? section.defaults.default_odcs_api_version
    : null;
const defaultReason = (section: SettingsSection): boolean =>
  section.defaults.require_lifecycle_reason === true;

function initialForm(section: SettingsSection): Form {
  const v = section.values;
  const warn =
    typeof v.mandatory_warn_days === "number"
      ? v.mandatory_warn_days
      : defaultWarn(section);
  return {
    warnDays: warn === null ? "" : String(warn),
    version:
      typeof v.default_odcs_api_version === "string"
        ? v.default_odcs_api_version
        : (defaultVersion(section) ?? ""),
    reason:
      typeof v.require_lifecycle_reason === "boolean"
        ? v.require_lifecycle_reason
        : defaultReason(section),
  };
}

function build(
  form: Form,
  section: SettingsSection,
): Built<Partial<Record<FieldName, string>>> {
  const errors: Partial<Record<FieldName, string>> = {};
  const values: Record<string, unknown> = {};
  const warnText = form.warnDays.trim();
  if (warnText !== "") {
    const warn = /^\d{1,3}$/.test(warnText) ? Number(warnText) : -1;
    if (warn < 0 || warn > WARN_MAX)
      errors.warnDays = `Enter a whole number of days from 0 to ${String(WARN_MAX)}.`;
    else if (warn !== defaultWarn(section)) values.mandatory_warn_days = warn;
  }
  if (form.version !== "" && form.version !== defaultVersion(section))
    values.default_odcs_api_version = form.version;
  if (form.reason !== defaultReason(section))
    values.require_lifecycle_reason = form.reason;
  return Object.keys(errors).length > 0 ? { errors } : { values };
}

function mapErrors(problem: Problem): {
  fields: Partial<Record<FieldName, string>>;
  rest: boolean;
} {
  const fields: Partial<Record<FieldName, string>> = {};
  if (problem.kind !== "problem" || problem.status !== 422)
    return { fields, rest: true };
  let rest = false;
  for (const error of problem.errors) {
    const field: FieldName | undefined =
      error.pointer === "/mandatory_warn_days"
        ? "warnDays"
        : error.pointer === "/default_odcs_api_version"
          ? "version"
          : undefined;
    if (field === undefined || fields[field] !== undefined) rest = true;
    else fields[field] = error.detail;
  }
  return { fields, rest };
}

function GovernanceForm({
  section,
  versions,
  onReload,
}: {
  section: SettingsSection;
  versions: string[];
  onReload: () => void;
}) {
  const save = useSaveSection();
  const [form, setForm] = useState<Form>(() => initialForm(section));
  const [errors, setErrors] = useState<Partial<Record<FieldName, string>>>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  // A new etag (after a save or a reload) means new stored values: take them, keep the note.
  const [seen, setSeen] = useState(section.etag);
  if (seen !== section.etag) {
    setSeen(section.etag);
    setForm(initialForm(section));
    setProblem(null);
  }

  const change = (patch: Partial<Form>) => {
    setForm((current) => ({ ...current, ...patch }));
    setSaved(null);
  };

  function submit(values: Record<string, unknown>, message: string) {
    setProblem(null);
    setErrors({});
    setSaved(null);
    save.mutate(
      { section: section.section, values, etag: section.etag },
      {
        onSuccess: () => {
          setSaved(message);
        },
        onError: (failure) => {
          const found = problemOf(failure);
          const mapped = mapErrors(found);
          setErrors(mapped.fields);
          setProblem(
            mapped.rest || Object.keys(mapped.fields).length === 0
              ? found
              : null,
          );
        },
      },
    );
  }

  const { onSubmit, resetField } = sectionHandlers({
    section,
    form,
    build,
    setErrors,
    submit,
    savedMessage: "Governance settings saved.",
  });

  const storedFor = (key: string) => section.values[key] !== undefined;
  const warnDefault = defaultWarn(section);
  const versionDefault = defaultVersion(section);
  const options = (versions.length > 0 ? versions : [BUILTIN_VERSION]).map(
    (v) => ({ value: v, label: v }),
  );

  return (
    <form className={styles.wide} noValidate onSubmit={onSubmit}>
      {problem ? (
        <SaveProblem
          problem={problem}
          onReload={onReload}
          fieldLabels={{
            "/mandatory_warn_days": "Grace period",
            "/default_odcs_api_version": "Default ODCS version",
          }}
        />
      ) : null}
      {saved ? (
        <Alert intent="success" title="Saved">
          {saved}
        </Alert>
      ) : null}

      <Card title="Mandatory rule sets">
        <div className={styles.form}>
          <TextField
            id="settings-gov-warn"
            label="Grace period (days)"
            helper="0 enforces new mandatory versions at once."
            inputMode="numeric"
            autoComplete="off"
            value={form.warnDays}
            error={errors.warnDays}
            onChange={(event) => {
              change({ warnDays: event.target.value });
            }}
          />
          <div className={styles.fieldMeta}>
            <span>
              Default:{" "}
              {warnDefault === null
                ? "set by the validation service"
                : `${String(warnDefault)} days`}
            </span>
            <Button
              variant="ghost"
              size="small"
              disabled={!storedFor("mandatory_warn_days")}
              aria-label="Reset to default for the grace period"
              onClick={() => {
                resetField(["mandatory_warn_days"], "Grace period reset.");
              }}
            >
              Reset to default
            </Button>
          </div>
        </div>
      </Card>

      <Card title="New contracts">
        <div className={styles.form}>
          <SelectField
            id="settings-gov-version"
            label="Default ODCS version"
            helper="New contracts and blank documents start with this apiVersion."
            value={form.version}
            options={options}
            {...(versionDefault === null
              ? { placeholder: "The service default" }
              : {})}
            error={errors.version}
            onChange={(event) => {
              change({ version: event.target.value });
            }}
          />
          <div className={styles.fieldMeta}>
            <span>Default: {versionDefault ?? BUILTIN_VERSION}</span>
            <Button
              variant="ghost"
              size="small"
              disabled={!storedFor("default_odcs_api_version")}
              aria-label="Reset to default for the ODCS version"
              onClick={() => {
                resetField(
                  ["default_odcs_api_version"],
                  "Default ODCS version reset.",
                );
              }}
            >
              Reset to default
            </Button>
          </div>
        </div>
      </Card>

      <Card title="Lifecycle changes">
        <div className={styles.form}>
          <Checkbox
            id="settings-gov-reason"
            label="Require a reason to unpublish or deprecate"
            helper="The reason is recorded in the audit trail and sent to subscriber cards."
            checked={form.reason}
            onChange={(event) => {
              change({ reason: event.target.checked });
            }}
          />
          <div className={styles.fieldMeta}>
            <span>
              Default: {defaultReason(section) ? "required" : "optional"}
            </span>
            <Button
              variant="ghost"
              size="small"
              disabled={!storedFor("require_lifecycle_reason")}
              aria-label="Reset to default for the reason requirement"
              onClick={() => {
                resetField(
                  ["require_lifecycle_reason"],
                  "Reason requirement reset.",
                );
              }}
            >
              Reset to default
            </Button>
          </div>
        </div>
      </Card>

      <div className={styles.actions}>
        <Button
          type="submit"
          variant="primary"
          size="small"
          busy={save.isPending}
          busyLabel="Saving…"
        >
          Save governance settings
        </Button>
      </div>
    </form>
  );
}

export function GovernanceTab({
  section,
  choices,
  onReload,
}: {
  section: SettingsSection;
  choices: SettingsOut["choices"];
  onReload: () => void;
}) {
  return (
    <GovernanceForm
      section={section}
      versions={choices.odcs_api_versions}
      onReload={onReload}
    />
  );
}
