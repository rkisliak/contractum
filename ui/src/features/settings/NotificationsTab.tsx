// Settings > Notifications (quick 261120-0yd: N1 to N6). One form over the whole section: email
// templates per event type (subject and body with allow-listed {placeholders}, a server-rendered
// preview, a per-template reset), the sender, the delivery retries, the sunset reminder lead times
// and the webhook host list. HTTPS-only is enforced and shown, never editable. Saving sends the
// whole section with `If-Match` (the 0yc pattern); stored values are overrides, so a field that
// equals its default is simply not stored. Template text is plain text in the normal font.
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import type { NotificationPreviewIn, SettingsSection } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import {
  nameRuleError,
  SelectField,
  TextareaField,
  TextField,
} from "@/components/form";
import { problemOf } from "@/lib/unexpected";
import { usePreviewTemplate, useSaveSection } from "./api";
import { SaveProblem } from "./SaveProblem";
import { SendTestEmailDialog } from "./SendTestEmailDialog";
import styles from "./settings.module.css";

export const EVENT_OPTIONS = [
  { value: "contract.version.published", label: "Version published" },
  {
    value: "contract.version.breaking_published",
    label: "Published with breaking changes",
  },
  { value: "contract.latest.changed", label: "Latest version changed" },
  { value: "contract.version.deprecated", label: "Version deprecated" },
  { value: "contract.version.sunset_approaching", label: "Sunset approaching" },
  { value: "contract.version.unpublish_blocked", label: "Unpublish blocked" },
  { value: "card.subscription.removed", label: "Pin removed" },
  { value: "generic", label: "Other notices" },
] as const;

// The placeholders the API accepts (dc_core.platform_settings): the subject list, and the body
// list which adds the message parts. The API refuses any other name with a field pointer.
const SUBJECT_PLACEHOLDERS = [
  "odcs",
  "semver",
  "card",
  "event",
  "status",
  "sunset_date",
  "lead_days",
  "deprecated_at",
  "reason",
  "previous_latest",
];
const BODY_PLACEHOLDERS = [
  ...SUBJECT_PLACEHOLDERS,
  "subject",
  "details",
  "breaking_changes",
  "contract_id",
  "notice_id",
  "occurred_at",
];

const BUILTIN_DELAYS = [60, 300, 1800, 7200, 21600, 43200];
const BUILTIN_ATTEMPTS = 7;
const BUILTIN_LEADS = [7, 30];

type Draft = { subject: string; body: string };
type Form = {
  event: string;
  drafts: Record<string, Draft>;
  senderName: string;
  senderAddress: string;
  delays: string;
  attempts: string;
  leads: string;
  hosts: string;
};
type FieldName =
  | "subject"
  | "body"
  | "senderName"
  | "senderAddress"
  | "delays"
  | "attempts"
  | "leads"
  | "hosts";

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const numbersOf = (v: unknown, fallback: number[]): number[] =>
  Array.isArray(v) && v.every((n) => typeof n === "number") ? v : fallback;

/** "1 min, 5 min, 30 min": a human reading of delays in seconds. */
export function humanSeconds(total: number): string {
  if (total < 60) return `${String(total)} s`;
  const units: [string, number][] = [
    ["day", 86400],
    ["h", 3600],
    ["min", 60],
  ];
  const parts: string[] = [];
  let rest = total;
  for (const [name, size] of units) {
    const count = Math.floor(rest / size);
    if (count > 0) {
      parts.push(
        name === "day"
          ? `${String(count)} ${count === 1 ? "day" : "days"}`
          : `${String(count)} ${name}`,
      );
      rest -= count * size;
    }
  }
  if (rest > 0) parts.push(`${String(rest)} s`);
  return parts.join(" ");
}

const echo = (values: number[]) => values.map(humanSeconds).join(", ");

function defaultDraft(defaults: Record<string, unknown>, event: string): Draft {
  const all = defaults.email_templates;
  const entry = isRecord(all) ? all[event] : undefined;
  const found = isRecord(entry) ? entry : {};
  return {
    subject: typeof found.subject === "string" ? found.subject : "",
    body: typeof found.body === "string" ? found.body : "",
  };
}

function initialForm(section: SettingsSection, event?: string): Form {
  const stored = section.values;
  const templates = isRecord(stored.email_templates)
    ? stored.email_templates
    : {};
  const drafts: Record<string, Draft> = {};
  for (const { value } of EVENT_OPTIONS) {
    const base = defaultDraft(section.defaults, value);
    const own = isRecord(templates[value]) ? templates[value] : {};
    drafts[value] = {
      subject: typeof own.subject === "string" ? own.subject : base.subject,
      body: typeof own.body === "string" ? own.body : base.body,
    };
  }
  const sender = isRecord(stored.sender) ? stored.sender : {};
  const delays = numbersOf(stored.retry_schedule_s, [
    ...numbersOf(section.defaults.retry_schedule_s, BUILTIN_DELAYS),
  ]);
  const leads = numbersOf(
    stored.sunset_lead_days,
    numbersOf(section.defaults.sunset_lead_days, BUILTIN_LEADS),
  );
  const attempts =
    typeof stored.max_attempts === "number"
      ? stored.max_attempts
      : typeof section.defaults.max_attempts === "number"
        ? section.defaults.max_attempts
        : BUILTIN_ATTEMPTS;
  return {
    event: event ?? EVENT_OPTIONS[0].value,
    drafts,
    senderName: typeof sender.name === "string" ? sender.name : "",
    senderAddress: typeof sender.address === "string" ? sender.address : "",
    delays: delays.join(", "),
    attempts: String(attempts),
    leads: leads.join(", "),
    hosts: Array.isArray(stored.webhook_allowed_hosts)
      ? stored.webhook_allowed_hosts.join("\n")
      : "",
  };
}

/** Whole numbers separated by commas; `null` when a piece is not a whole number. */
function parseNumbers(text: string): number[] | null {
  const pieces = text
    .split(",")
    .map((p) => p.trim())
    .filter((p) => p !== "");
  if (pieces.length === 0 || !pieces.every((p) => /^\d{1,9}$/.test(p)))
    return null;
  return pieces.map(Number);
}

const same = (a: number[], b: number[]) =>
  a.length === b.length && a.every((n, i) => n === b[i]);

type Built =
  | { values: Record<string, unknown>; errors?: undefined }
  | { errors: Partial<Record<FieldName, string>>; values?: undefined };

function build(form: Form, section: SettingsSection): Built {
  const errors: Partial<Record<FieldName, string>> = {};
  const values: Record<string, unknown> = {};
  const templates: Record<string, Record<string, string>> = {};
  for (const { value } of EVENT_OPTIONS) {
    const base = defaultDraft(section.defaults, value);
    const draft = form.drafts[value] ?? base;
    if (draft.subject.trim() === "") {
      errors.subject = "Enter a subject, or reset the template to default.";
    }
    if (draft.body.trim() === "") {
      errors.body = "Enter a body, or reset the template to default.";
    }
    const entry: Record<string, string> = {};
    if (draft.subject !== base.subject) entry.subject = draft.subject;
    if (draft.body !== base.body) entry.body = draft.body;
    if (Object.keys(entry).length > 0) templates[value] = entry;
  }
  if (Object.keys(templates).length > 0) values.email_templates = templates;

  const name = form.senderName.trim();
  const address = form.senderAddress.trim();
  const refusedName = nameRuleError(form.senderName);
  if (refusedName !== undefined) errors.senderName = refusedName;
  if (name !== "" || address !== "") {
    values.sender = {
      ...(name !== "" ? { name } : {}),
      ...(address !== "" ? { address } : {}),
    };
  }

  const delays = parseNumbers(form.delays);
  if (delays === null)
    errors.delays =
      "Enter whole numbers of seconds separated by commas, such as 60, 300, 1800.";
  else if (
    !same(delays, numbersOf(section.defaults.retry_schedule_s, BUILTIN_DELAYS))
  )
    values.retry_schedule_s = delays;

  const attempts = form.attempts.trim();
  if (!/^\d{1,3}$/.test(attempts))
    errors.attempts = "Enter a whole number of attempts.";
  else if (
    Number(attempts) !==
    (typeof section.defaults.max_attempts === "number"
      ? section.defaults.max_attempts
      : BUILTIN_ATTEMPTS)
  )
    values.max_attempts = Number(attempts);

  const leads = parseNumbers(form.leads);
  if (leads === null)
    errors.leads =
      "Enter whole numbers of days separated by commas, such as 30, 7.";
  else if (
    !same(
      [...leads].sort((a, b) => a - b),
      [...numbersOf(section.defaults.sunset_lead_days, BUILTIN_LEADS)].sort(
        (a, b) => a - b,
      ),
    )
  )
    values.sunset_lead_days = leads;

  const hosts = form.hosts
    .split("\n")
    .map((h) => h.trim())
    .filter((h) => h !== "");
  if (hosts.length > 0) values.webhook_allowed_hosts = hosts;

  return Object.keys(errors).length > 0 ? { errors } : { values };
}

/** Server pointers onto the fields; a template error also selects the event it belongs to. */
function mapErrors(problem: Problem): {
  fields: Partial<Record<FieldName, string>>;
  event?: string;
  rest: boolean;
} {
  const fields: Partial<Record<FieldName, string>> = {};
  let event: string | undefined;
  let rest = problem.kind !== "problem" || problem.status !== 422;
  if (problem.kind !== "problem" || problem.status !== 422) {
    return { fields, rest };
  }
  for (const error of problem.errors) {
    const pointer = error.pointer;
    const template = /^\/email_templates\/([^/]+)\/(subject|body)$/.exec(
      pointer,
    );
    let field: FieldName | undefined;
    if (template?.[1] !== undefined && template[2] !== undefined) {
      field = template[2] as FieldName;
      event = template[1].replace(/~1/g, "/").replace(/~0/g, "~");
    } else if (pointer === "/sender/name") field = "senderName";
    else if (pointer === "/sender/address") field = "senderAddress";
    else if (pointer.startsWith("/retry_schedule_s")) field = "delays";
    else if (pointer === "/max_attempts") field = "attempts";
    else if (pointer.startsWith("/sunset_lead_days")) field = "leads";
    else if (pointer.startsWith("/webhook_allowed_hosts")) field = "hosts";
    if (field === undefined || fields[field] !== undefined) rest = true;
    else fields[field] = error.detail;
  }
  return { fields, event, rest };
}

function PlaceholderList({ names }: { names: string[] }) {
  return (
    <ul className={styles.placeholders} aria-label="Placeholders you can use">
      {names.map((name) => (
        <li key={name}>
          <code>{`{${name}}`}</code>
        </li>
      ))}
    </ul>
  );
}

function NotificationsForm({
  section,
  onReload,
}: {
  section: SettingsSection;
  onReload: () => void;
}) {
  const save = useSaveSection();
  const preview = usePreviewTemplate();
  const [form, setForm] = useState<Form>(() => initialForm(section));
  const [errors, setErrors] = useState<Partial<Record<FieldName, string>>>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [testOpen, setTestOpen] = useState(false);
  // A new etag (after a save or a reload) means new stored values: take them, keep the "saved"
  // note and the selected event. State adjusted during render, so nothing remounts.
  const [seen, setSeen] = useState(section.etag);
  if (seen !== section.etag) {
    setSeen(section.etag);
    setForm((current) => initialForm(section, current.event));
    setProblem(null);
  }

  const defaults = section.defaults;
  const baseDraft = defaultDraft(defaults, form.event);
  const draft = form.drafts[form.event] ?? baseDraft;
  const senderDefault = isRecord(defaults.sender) ? defaults.sender : {};
  const defaultAddress =
    typeof senderDefault.address === "string" ? senderDefault.address : null;
  const defaultDelays = numbersOf(defaults.retry_schedule_s, BUILTIN_DELAYS);
  const defaultAttempts =
    typeof defaults.max_attempts === "number"
      ? defaults.max_attempts
      : BUILTIN_ATTEMPTS;
  const defaultLeads = numbersOf(defaults.sunset_lead_days, BUILTIN_LEADS);
  const storedTemplates = isRecord(section.values.email_templates)
    ? section.values.email_templates
    : {};
  const delaysParsed = parseNumbers(form.delays);

  const change = (patch: Partial<Form>) => {
    setForm((current) => ({ ...current, ...patch }));
    setSaved(null);
    preview.reset();
  };
  const setDraft = (patch: Partial<Draft>) => {
    setForm((current) => ({
      ...current,
      drafts: {
        ...current.drafts,
        [current.event]: {
          ...(current.drafts[current.event] ?? baseDraft),
          ...patch,
        },
      },
    }));
    setSaved(null);
    preview.reset();
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
          if (mapped.event !== undefined) change({ event: mapped.event });
          setProblem(
            mapped.rest || Object.keys(mapped.fields).length === 0
              ? found
              : null,
          );
        },
      },
    );
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const built = build(form, section);
    if (built.errors !== undefined) {
      setErrors(built.errors);
      return;
    }
    submit(built.values, "Notification settings saved.");
  }

  function resetTemplate() {
    // Saves the stored values without this template; other unsaved edits stay in the form.
    const kept = Object.fromEntries(
      Object.entries(storedTemplates).filter(([key]) => key !== form.event),
    );
    const next: Record<string, unknown> = { ...section.values };
    if (Object.keys(kept).length > 0) next.email_templates = kept;
    else delete next.email_templates;
    setForm((current) => ({
      ...current,
      drafts: { ...current.drafts, [current.event]: baseDraft },
    }));
    submit(next, "Template reset to default.");
  }

  function resetField(keys: string[], message: string) {
    const next = Object.fromEntries(
      Object.entries(section.values).filter(([key]) => !keys.includes(key)),
    );
    submit(next, message);
  }

  function runPreview() {
    setErrors({});
    preview.mutate(
      {
        event_type: form.event as NotificationPreviewIn["event_type"],
        subject: draft.subject,
        body: draft.body,
      },
      {
        onError: (failure) => {
          const found = problemOf(failure);
          if (found.kind === "problem" && found.status === 422) {
            const next: Partial<Record<FieldName, string>> = {};
            for (const error of found.errors) {
              if (error.pointer === "/subject") next.subject = error.detail;
              if (error.pointer === "/body") next.body = error.detail;
            }
            setErrors(next);
          }
        },
      },
    );
  }

  const previewProblem =
    preview.isError &&
    !(
      problemOf(preview.error).kind === "problem" &&
      problemOf(preview.error).status === 422
    )
      ? problemOf(preview.error)
      : null;
  const storedFor = (key: string) => section.values[key] !== undefined;
  const hostsList = form.hosts
    .split("\n")
    .map((h) => h.trim())
    .filter((h) => h !== "");
  const isStoredTemplate = isRecord(storedTemplates[form.event]);

  return (
    <form className={styles.wide} noValidate onSubmit={onSubmit}>
      {problem ? (
        <SaveProblem
          problem={problem}
          onReload={onReload}
          fieldLabels={{
            "/sender/name": "Sender name",
            "/sender/address": "Sender address",
            "/retry_schedule_s": "Retry delays",
            "/max_attempts": "Maximum attempts",
            "/sunset_lead_days": "Sunset reminders",
            "/webhook_allowed_hosts": "Allowed webhook hosts",
          }}
        />
      ) : null}
      {saved ? (
        <Alert intent="success" title="Saved">
          {saved}
        </Alert>
      ) : null}

      <Card title="Email templates">
        <div className={styles.measure}>
          <SelectField
            id="settings-notify-event"
            label="Notice"
            helper="Each notice type has its own subject and body."
            value={form.event}
            options={EVENT_OPTIONS.map((o) => ({
              value: o.value,
              label: o.label,
            }))}
            onChange={(event) => {
              setErrors({});
              change({ event: event.target.value });
            }}
          />
          <TextField
            id="settings-notify-subject"
            label="Subject"
            autoComplete="off"
            value={draft.subject}
            error={errors.subject}
            onChange={(event) => {
              setDraft({ subject: event.target.value });
            }}
          />
          <PlaceholderList names={SUBJECT_PLACEHOLDERS} />
          <TextareaField
            id="settings-notify-body"
            label="Body"
            helper="Plain text. Use {{ and }} for a literal brace."
            rows={10}
            value={draft.body}
            error={errors.body}
            onChange={(event) => {
              setDraft({ body: event.target.value });
            }}
          />
          <PlaceholderList names={BODY_PLACEHOLDERS} />
          <div className={styles.actions}>
            <Button
              variant="outline-neutral"
              size="small"
              busy={preview.isPending}
              busyLabel="Rendering…"
              onClick={runPreview}
            >
              Preview
            </Button>
            <Button
              variant="ghost"
              size="small"
              disabled={!isStoredTemplate}
              aria-label="Reset to default for this template"
              onClick={resetTemplate}
            >
              Reset to default
            </Button>
          </div>
          {previewProblem ? (
            <Alert intent="warning" title="The preview could not run">
              Try again shortly.
            </Alert>
          ) : null}
          {preview.data ? (
            <div className={styles.previewBox} data-testid="template-preview">
              <p className={styles.subhead}>Preview with sample values</p>
              <p className={styles.previewText}>
                <strong>{preview.data.subject}</strong>
              </p>
              <p className={styles.previewText}>{preview.data.body}</p>
            </div>
          ) : null}
        </div>
      </Card>

      <Card title="Sender">
        <div className={styles.measure}>
          <div className={styles.form}>
            <TextField
              id="settings-notify-sender-name"
              label="Sender name"
              optional
              autoComplete="off"
              value={form.senderName}
              error={errors.senderName ?? nameRuleError(form.senderName)}
              onChange={(event) => {
                change({ senderName: event.target.value });
              }}
            />
            <TextField
              id="settings-notify-sender-address"
              label="Sender address"
              type="email"
              optional
              autoComplete="off"
              value={form.senderAddress}
              error={errors.senderAddress}
              onChange={(event) => {
                change({ senderAddress: event.target.value });
              }}
            />
            <div className={styles.fieldMeta}>
              <span>
                Default:{" "}
                <span className={styles.ident}>
                  {defaultAddress ?? "the mail server's address"}
                </span>
              </span>
              <Button
                variant="ghost"
                size="small"
                disabled={!storedFor("sender")}
                aria-label="Reset to default for sender"
                onClick={() => {
                  resetField(["sender"], "Sender reset to default.");
                }}
              >
                Reset to default
              </Button>
            </div>
          </div>
          <div className={styles.actions}>
            <Button
              variant="outline-neutral"
              size="small"
              onClick={() => {
                setTestOpen(true);
              }}
            >
              Send test email
            </Button>
          </div>
        </div>
      </Card>

      <Card title="Delivery retries">
        <div className={styles.form}>
          <TextField
            id="settings-notify-delays"
            label="Retry delays (seconds)"
            helper={
              delaysParsed
                ? `Wait after each failed attempt: ${echo(delaysParsed)}. The last delay repeats.`
                : "Seconds, separated by commas."
            }
            autoComplete="off"
            value={form.delays}
            error={errors.delays}
            onChange={(event) => {
              change({ delays: event.target.value });
            }}
          />
          <TextField
            id="settings-notify-attempts"
            label="Maximum attempts"
            inputMode="numeric"
            autoComplete="off"
            value={form.attempts}
            error={errors.attempts}
            onChange={(event) => {
              change({ attempts: event.target.value });
            }}
          />
          <div className={styles.fieldMeta}>
            <span>
              Default: {echo(defaultDelays)}, up to {String(defaultAttempts)}{" "}
              attempts
            </span>
            <Button
              variant="ghost"
              size="small"
              disabled={
                !storedFor("retry_schedule_s") && !storedFor("max_attempts")
              }
              aria-label="Reset to default for retries"
              onClick={() => {
                resetField(
                  ["retry_schedule_s", "max_attempts"],
                  "Retries reset to default.",
                );
              }}
            >
              Reset to default
            </Button>
          </div>
        </div>
      </Card>

      <Card title="Sunset reminders">
        <div className={styles.form}>
          <TextField
            id="settings-notify-leads"
            label="Reminder lead times (days before sunset)"
            helper="Each pinned card is reminded once per lead time, for example 30, 7."
            autoComplete="off"
            value={form.leads}
            error={errors.leads}
            onChange={(event) => {
              change({ leads: event.target.value });
            }}
          />
          <div className={styles.fieldMeta}>
            <span>Default: {defaultLeads.join(", ")} days</span>
            <Button
              variant="ghost"
              size="small"
              disabled={!storedFor("sunset_lead_days")}
              aria-label="Reset to default for sunset reminders"
              onClick={() => {
                resetField(["sunset_lead_days"], "Reminders reset to default.");
              }}
            >
              Reset to default
            </Button>
          </div>
        </div>
      </Card>

      <Card title="Webhook security">
        <div className={styles.form}>
          <div className={styles.fieldMeta}>
            <span>HTTPS only</span>
            <Badge intent="success">Enforced</Badge>
          </div>
          {hostsList.length === 0 ? (
            <Alert intent="warning" title="No webhook host is allowed">
              No webhook host is allowed, so card owners cannot add webhooks and
              none are sent.
            </Alert>
          ) : null}
          <TextareaField
            id="settings-notify-hosts"
            label="Allowed webhook hosts"
            helper="Webhooks are sent only to these hosts, one per line (hooks.example.com or *.example.com)."
            rows={4}
            value={form.hosts}
            error={errors.hosts}
            placeholder={"hooks.example.com\n*.example.org"}
            onChange={(event) => {
              change({ hosts: event.target.value });
            }}
          />
          <div className={styles.fieldMeta}>
            <span>
              {hostsList.length === 0
                ? "Default: no host is allowed"
                : `${String(hostsList.length)} ${hostsList.length === 1 ? "host" : "hosts"} listed. One per line; *. matches every subdomain.`}
            </span>
            <Button
              variant="ghost"
              size="small"
              disabled={!storedFor("webhook_allowed_hosts")}
              aria-label="Reset to default for allowed hosts"
              onClick={() => {
                resetField(
                  ["webhook_allowed_hosts"],
                  "Allowed hosts reset to default.",
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
          Save notification settings
        </Button>
      </div>
      <SendTestEmailDialog open={testOpen} onOpenChange={setTestOpen} />
    </form>
  );
}

export function NotificationsTab({
  section,
  onReload,
}: {
  section: SettingsSection;
  choices?: unknown;
  onReload: () => void;
}) {
  return <NotificationsForm section={section} onReload={onReload} />;
}
