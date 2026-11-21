// Settings > Sign-in & security (quick 261120-0ye: S1 to S5). Sign-in methods with the lockout
// safety, the longest personal-token and key lifetime, session length and idle timeout, login
// lockout and password rules. One form over the whole section; saving sends it with `If-Match`.
// A field equal to its default is not stored. Hours, minutes and days convert to the API's units
// in `units.ts`. The server refuses any change that could lock everyone out
// (`signin-lockout-risk`): its message is shown as a warning and nothing changes.
import { useState } from "react";
import type { Problem } from "@/api/problem";
import type { SettingsOut, SettingsSection } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { Checkbox, TextField } from "@/components/form";
import { problemOf } from "@/lib/unexpected";
import { useSaveSection } from "./api";
import { SaveProblem } from "./SaveProblem";
import { sectionHandlers, type Built } from "./sectionHandlers";
import styles from "./settings.module.css";
import { fromDisplay, rangeHint, toDisplay, type Unit } from "./units";

type NumSpec = {
  key: string;
  id: string;
  label: string;
  unit: Unit;
  /** Bounds in the API's unit. */
  min: number;
  max: number;
  helper?: string;
  /** The word after "Default:" ("days", "attempts"). */
  suffix: string;
};

const NUMBERS: Record<string, NumSpec[]> = {
  tokens: [
    {
      key: "credential_max_ttl_days",
      id: "settings-sec-ttl",
      label: "Longest lifetime (days)",
      unit: "days",
      min: 1,
      max: 3650,
      helper:
        "The furthest expiry a personal access token or service-account key may have. Existing ones are unchanged.",
      suffix: "days",
    },
    {
      key: "max_active_tokens",
      id: "settings-sec-max-tokens",
      label: "Maximum active access tokens per owner",
      unit: "count",
      min: 1,
      max: 1000,
      helper:
        "Each user, and each service account, may hold at most this many active tokens. Existing tokens above the limit are kept.",
      suffix: "tokens",
    },
  ],
  sessions: [
    {
      key: "max_sessions_per_user",
      id: "settings-sec-max-sessions",
      label: "Maximum sessions per user",
      unit: "count",
      min: 1,
      max: 100,
      helper:
        "Signing in beyond this ends the user's oldest session. Existing sessions above a lowered limit end at that user's next sign-in.",
      suffix: "sessions",
    },
    {
      key: "session_max_s",
      id: "settings-sec-session",
      label: "Session length (hours)",
      unit: "hours",
      min: 3600,
      max: 7776000,
      helper: "A browser session ends this long after sign-in, however active.",
      suffix: "h",
    },
    {
      key: "idle_timeout_s",
      id: "settings-sec-idle",
      label: "Idle timeout (minutes)",
      unit: "minutes",
      min: 900,
      max: 604800,
      helper:
        "A session with no activity for this long ends. At most the session length.",
      suffix: "min",
    },
  ],
  lockout: [
    {
      key: "lockout_account_threshold",
      id: "settings-sec-lock-account",
      label: "Failed attempts per account",
      unit: "count",
      min: 3,
      max: 20,
      suffix: "attempts",
    },
    {
      key: "lockout_ip_threshold",
      id: "settings-sec-lock-ip",
      label: "Failed attempts per address",
      unit: "count",
      min: 10,
      max: 500,
      suffix: "attempts",
    },
    {
      key: "lockout_max_lock_s",
      id: "settings-sec-lock-max",
      label: "Longest lock (minutes)",
      unit: "minutes",
      min: 60,
      max: 86400,
      helper: "Each further failure doubles the lock, up to this.",
      suffix: "min",
    },
  ],
  passwords: [
    {
      key: "password_min_length",
      id: "settings-sec-pw-length",
      label: "Minimum length",
      unit: "count",
      min: 12,
      max: 128,
      suffix: "characters",
    },
  ],
};
const ALL_NUMBERS = Object.values(NUMBERS).flat();

const CLASS_RULES = [
  [
    "password_require_upper",
    "settings-sec-pw-upper",
    "Require an upper-case letter",
  ],
  [
    "password_require_lower",
    "settings-sec-pw-lower",
    "Require a lower-case letter",
  ],
  ["password_require_digit", "settings-sec-pw-digit", "Require a digit"],
  ["password_require_symbol", "settings-sec-pw-symbol", "Require a symbol"],
] as const;
const BOOLS = [
  "local_signin_enabled",
  "microsoft_signin_enabled",
  ...CLASS_RULES.map(([key]) => key),
] as const;
type BoolKey = (typeof BOOLS)[number];

type Form = { nums: Record<string, string>; bools: Record<BoolKey, boolean> };
type Errors = Record<string, string>;

const defaultNumber = (
  section: SettingsSection,
  key: string,
): number | null => {
  const value = section.defaults[key];
  return typeof value === "number" ? value : null;
};
const defaultBool = (section: SettingsSection, key: BoolKey): boolean => {
  const value = section.defaults[key];
  return typeof value === "boolean" ? value : key.endsWith("_enabled");
};

function initialForm(section: SettingsSection): Form {
  const nums: Record<string, string> = {};
  for (const spec of ALL_NUMBERS) {
    const stored = section.values[spec.key];
    const value =
      typeof stored === "number" ? stored : defaultNumber(section, spec.key);
    nums[spec.key] = value === null ? "" : toDisplay(value, spec.unit);
  }
  const bools = {} as Record<BoolKey, boolean>;
  for (const key of BOOLS) {
    const stored = section.values[key];
    bools[key] =
      typeof stored === "boolean" ? stored : defaultBool(section, key);
  }
  return { nums, bools };
}

function build(form: Form, section: SettingsSection): Built<Errors> {
  const errors: Errors = {};
  const values: Record<string, unknown> = {};
  for (const spec of ALL_NUMBERS) {
    const text = form.nums[spec.key] ?? "";
    if (text.trim() === "") continue;
    const parsed = fromDisplay(text, spec.unit);
    if (parsed === null || parsed < spec.min || parsed > spec.max) {
      errors[spec.key] =
        `Enter a number from ${rangeHint(spec.min, spec.max, spec.unit)}.`;
    } else if (parsed !== defaultNumber(section, spec.key)) {
      values[spec.key] = parsed;
    }
  }
  const idle = fromDisplay(form.nums.idle_timeout_s ?? "", "minutes");
  const session = fromDisplay(form.nums.session_max_s ?? "", "hours");
  if (
    idle !== null &&
    session !== null &&
    idle > session &&
    !errors.idle_timeout_s
  )
    errors.idle_timeout_s =
      "The idle timeout must not exceed the session length.";
  for (const key of BOOLS) {
    if (form.bools[key] !== defaultBool(section, key))
      values[key] = form.bools[key];
  }
  return Object.keys(errors).length > 0 ? { errors } : { values };
}

function mapErrors(problem: Problem): { fields: Errors; rest: boolean } {
  const fields: Errors = {};
  if (problem.kind !== "problem" || problem.status !== 422)
    return { fields, rest: true };
  let rest = false;
  for (const error of problem.errors) {
    const key = error.pointer.replace(/^\//, "");
    const known =
      ALL_NUMBERS.some((spec) => spec.key === key) ||
      (BOOLS as readonly string[]).includes(key);
    if (!known || fields[key] !== undefined) rest = true;
    else fields[key] = error.detail;
  }
  return { fields, rest };
}

/** The refusal that explains a lockout risk, shown as a warning (slug `signin-lockout-risk`). */
export function lockoutDetail(problem: Problem): string | null {
  return problem.kind === "problem" && problem.slug === "signin-lockout-risk"
    ? (problem.detail ?? "This change could lock every admin out.")
    : null;
}

function NumberField({
  spec,
  form,
  errors,
  section,
  onChange,
}: {
  spec: NumSpec;
  form: Form;
  errors: Errors;
  section: SettingsSection;
  onChange: (key: string, value: string) => void;
}) {
  const fallback = defaultNumber(section, spec.key);
  return (
    <>
      <TextField
        id={spec.id}
        label={spec.label}
        {...(spec.helper === undefined ? {} : { helper: spec.helper })}
        inputMode="decimal"
        autoComplete="off"
        value={form.nums[spec.key] ?? ""}
        error={errors[spec.key]}
        onChange={(event) => {
          onChange(spec.key, event.target.value);
        }}
      />
      <span className={styles.counter}>
        Default:{" "}
        {fallback === null
          ? "set by the service"
          : `${toDisplay(fallback, spec.unit)} ${spec.suffix}`}
        . Allowed: {rangeHint(spec.min, spec.max, spec.unit)}.
      </span>
    </>
  );
}

function SecurityForm({
  section,
  microsoftAvailable,
  onReload,
}: {
  section: SettingsSection;
  microsoftAvailable: boolean;
  onReload: () => void;
}) {
  const save = useSaveSection();
  const [form, setForm] = useState<Form>(() => initialForm(section));
  const [errors, setErrors] = useState<Errors>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const [lockout, setLockout] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [seen, setSeen] = useState(section.etag);
  if (seen !== section.etag) {
    setSeen(section.etag);
    setForm(initialForm(section));
    setProblem(null);
  }

  const edit = (next: (current: Form) => Form) => {
    setForm(next);
    setSaved(null);
    setLockout(null);
  };
  const setNum = (key: string, value: string) => {
    edit((current) => ({
      ...current,
      nums: { ...current.nums, [key]: value },
    }));
  };
  const setBool = (key: BoolKey, value: boolean) => {
    edit((current) => ({
      ...current,
      bools: { ...current.bools, [key]: value },
    }));
  };

  function submit(values: Record<string, unknown>, message: string) {
    setProblem(null);
    setErrors({});
    setSaved(null);
    setLockout(null);
    save.mutate(
      { section: section.section, values, etag: section.etag },
      {
        onSuccess: () => {
          setSaved(message);
        },
        onError: (failure) => {
          const found = problemOf(failure);
          const refusal = lockoutDetail(found);
          if (refusal !== null) {
            setLockout(refusal);
            return;
          }
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
    savedMessage: "Sign-in and security settings saved.",
  });
  const storedAny = (keys: string[]) =>
    keys.some((key) => section.values[key] !== undefined);
  const resetRow = (label: string, keys: string[], message: string) => (
    <div className={styles.fieldMeta}>
      <Button
        variant="ghost"
        size="small"
        disabled={!storedAny(keys)}
        aria-label={`Reset to default for ${label}`}
        onClick={() => {
          resetField(keys, message);
        }}
      >
        Reset to default
      </Button>
    </div>
  );
  const numbers = (group: keyof typeof NUMBERS) =>
    (NUMBERS[group] ?? []).map((spec) => (
      <NumberField
        key={spec.key}
        spec={spec}
        form={form}
        errors={errors}
        section={section}
        onChange={setNum}
      />
    ));
  const keysOf = (group: keyof typeof NUMBERS) =>
    (NUMBERS[group] ?? []).map((spec) => spec.key);

  return (
    <form className={styles.wide} noValidate onSubmit={onSubmit}>
      {lockout !== null ? (
        <Alert intent="warning" title="This change could lock you out">
          {lockout} Nothing was changed.
        </Alert>
      ) : null}
      {problem ? (
        <SaveProblem
          problem={problem}
          onReload={onReload}
          fieldLabels={Object.fromEntries(
            ALL_NUMBERS.map((spec) => [`/${spec.key}`, spec.label]),
          )}
        />
      ) : null}
      {saved ? (
        <Alert intent="success" title="Saved">
          {saved}
        </Alert>
      ) : null}

      <Card title="Sign-in methods">
        <div className={styles.form}>
          <Checkbox
            id="settings-sec-local"
            label="Local sign-in"
            helper="Username and password accounts. Personal tokens, keys and service accounts are not sign-ins and keep working."
            checked={form.bools.local_signin_enabled}
            error={errors.local_signin_enabled}
            onChange={(event) => {
              setBool("local_signin_enabled", event.target.checked);
            }}
          />
          <Checkbox
            id="settings-sec-microsoft"
            label="Microsoft sign-in"
            helper={
              microsoftAvailable
                ? "Microsoft Entra accounts."
                : "Microsoft sign-in is not configured for this deployment."
            }
            checked={microsoftAvailable && form.bools.microsoft_signin_enabled}
            disabled={!microsoftAvailable}
            error={errors.microsoft_signin_enabled}
            onChange={(event) => {
              setBool("microsoft_signin_enabled", event.target.checked);
            }}
          />
          <span className={styles.counter}>
            At least one method stays on, and you cannot turn off the one you
            are signed in with. Sessions already open end within their normal
            lifetime.
          </span>
          {resetRow(
            "sign-in methods",
            ["local_signin_enabled", "microsoft_signin_enabled"],
            "Sign-in methods reset to default.",
          )}
        </div>
      </Card>

      <Card title="Access tokens">
        <div className={styles.form}>
          {numbers("tokens")}
          {resetRow(
            "access tokens",
            keysOf("tokens"),
            "Access token settings reset.",
          )}
        </div>
      </Card>

      <Card title="Sessions">
        <div className={styles.form}>
          {numbers("sessions")}
          {resetRow("sessions", keysOf("sessions"), "Session limits reset.")}
        </div>
      </Card>

      <Card title="Login lockout">
        <div className={styles.form}>
          {numbers("lockout")}
          {resetRow(
            "login lockout",
            keysOf("lockout"),
            "Lockout settings reset.",
          )}
        </div>
      </Card>

      <Card title="Password rules">
        <div className={styles.form}>
          {numbers("passwords")}
          {CLASS_RULES.map(([key, id, label]) => (
            <Checkbox
              key={key}
              id={id}
              label={label}
              checked={form.bools[key]}
              error={errors[key]}
              onChange={(event) => {
                setBool(key, event.target.checked);
              }}
            />
          ))}
          <span className={styles.counter}>
            Applies to new passwords only. Existing passwords keep working.
          </span>
          {resetRow(
            "password rules",
            ["password_min_length", ...CLASS_RULES.map(([key]) => key)],
            "Password rules reset.",
          )}
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
          Save sign-in and security settings
        </Button>
      </div>
    </form>
  );
}

export function SecurityTab({
  section,
  choices,
  onReload,
}: {
  section: SettingsSection;
  choices: SettingsOut["choices"];
  onReload: () => void;
}) {
  return (
    <SecurityForm
      section={section}
      microsoftAvailable={choices.microsoft_signin_available}
      onReload={onReload}
    />
  );
}
