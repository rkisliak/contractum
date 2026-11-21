// The audit filter bar (D-UI-13-05): object type and id, actor (with a "Me" shortcut), category,
// service, action (exact or dotted prefix) and a half-open local time range with presets. Edits
// stay a draft until "Apply filters", then become the page's search params.
import { useState, type SubmitEvent } from "react";
import { Button } from "@/components/Button";
import { SelectField, TextField } from "@/components/form";
import {
  AGGREGATE_TYPES,
  CATEGORIES,
  PRESETS,
  SERVICES,
  fromLocalInput,
  hasFilters,
  presetRange,
  toLocalInput,
  type AuditCategory,
  type AuditFilters as Filters,
} from "./query";
import styles from "./Audit.module.css";

type Draft = Record<
  | "aggregate_type"
  | "aggregate_id"
  | "actor_id"
  | "service"
  | "action"
  | "category"
  | "from"
  | "to",
  string
>;

function draftOf(filters: Filters): Draft {
  return {
    aggregate_type: filters.aggregate_type ?? "",
    aggregate_id: filters.aggregate_id ?? "",
    actor_id: filters.actor_id ?? "",
    service: filters.service ?? "",
    action: filters.action ?? "",
    category: filters.category ?? "",
    from: toLocalInput(filters.from),
    to: toLocalInput(filters.to),
  };
}

function filtersOf(draft: Draft): Filters {
  const value = (s: string) => (s.trim() === "" ? undefined : s.trim());
  return {
    aggregate_type: value(draft.aggregate_type),
    aggregate_id: value(draft.aggregate_id),
    actor_id: value(draft.actor_id),
    service: value(draft.service),
    action: value(draft.action),
    category: (value(draft.category) as AuditCategory | undefined) ?? undefined,
    from: fromLocalInput(draft.from),
    to: fromLocalInput(draft.to),
  };
}

export function AuditFilters({
  filters,
  myPrincipalId,
  onApply,
}: {
  filters: Filters;
  myPrincipalId?: string;
  onApply: (filters: Filters) => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => draftOf(filters));
  const appliedKey = JSON.stringify(filters);
  const [applied, setApplied] = useState(appliedKey);
  // The URL changed (back button, a deep link): show what is actually applied.
  if (applied !== appliedKey) {
    setApplied(appliedKey);
    setDraft(draftOf(filters));
  }
  const set = (key: keyof Draft) => (value: string) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    onApply(filtersOf(draft));
  }

  return (
    <form
      role="search"
      aria-label="Audit filters"
      className={styles.filters}
      noValidate
      onSubmit={onSubmit}
    >
      <div className={styles.filterGrid}>
        <TextField
          id="audit-aggregate-type"
          label="Object type"
          autoComplete="off"
          controlSize="small"
          list="audit-aggregate-types"
          value={draft.aggregate_type}
          onChange={(event) => {
            set("aggregate_type")(event.target.value);
          }}
        />
        <datalist id="audit-aggregate-types">
          {AGGREGATE_TYPES.map((type) => (
            <option key={type} value={type} />
          ))}
        </datalist>
        <TextField
          id="audit-aggregate-id"
          label="Object id"
          autoComplete="off"
          controlSize="small"
          spellCheck={false}
          value={draft.aggregate_id}
          onChange={(event) => {
            set("aggregate_id")(event.target.value);
          }}
        />
        <div className={styles.withAction}>
          <TextField
            id="audit-actor"
            label="Actor id"
            autoComplete="off"
            controlSize="small"
            spellCheck={false}
            helper="For example local:… or entra:…"
            value={draft.actor_id}
            onChange={(event) => {
              set("actor_id")(event.target.value);
            }}
          />
          {myPrincipalId ? (
            <Button
              variant="outline-neutral"
              size="small"
              onClick={() => {
                set("actor_id")(myPrincipalId);
              }}
            >
              Me
            </Button>
          ) : null}
        </div>
        <SelectField
          id="audit-category"
          label="Category"
          controlSize="small"
          placeholder="Any category"
          options={CATEGORIES.map((category) => ({
            value: category,
            label: category.charAt(0).toUpperCase() + category.slice(1),
          }))}
          value={draft.category}
          onChange={(event) => {
            set("category")(event.target.value);
          }}
        />
        <SelectField
          id="audit-service"
          label="Service"
          controlSize="small"
          placeholder="Any service"
          options={SERVICES.map((service) => ({
            value: service,
            label: service,
          }))}
          value={draft.service}
          onChange={(event) => {
            set("service")(event.target.value);
          }}
        />
        <TextField
          id="audit-action"
          label="Action"
          autoComplete="off"
          controlSize="small"
          spellCheck={false}
          helper="Exact, or a dotted prefix such as contract.version."
          value={draft.action}
          onChange={(event) => {
            set("action")(event.target.value);
          }}
        />
        <TextField
          id="audit-from"
          label="From (local time)"
          type="datetime-local"
          autoComplete="off"
          controlSize="small"
          helper="Included."
          value={draft.from}
          onChange={(event) => {
            set("from")(event.target.value);
          }}
        />
        <TextField
          id="audit-to"
          label="To (local time)"
          type="datetime-local"
          autoComplete="off"
          controlSize="small"
          helper="Excluded: events before this moment."
          value={draft.to}
          onChange={(event) => {
            set("to")(event.target.value);
          }}
        />
      </div>
      <div className={styles.filterActions}>
        <div className={styles.presets} role="group" aria-label="Time presets">
          {PRESETS.map((preset) => (
            <Button
              key={preset.id}
              variant="ghost"
              size="small"
              onClick={() => {
                const range = presetRange(preset.ms);
                const next = { ...filtersOf(draft), ...range };
                setDraft(draftOf(next));
                onApply(next);
              }}
            >
              {preset.label}
            </Button>
          ))}
        </div>
        <div className={styles.presets}>
          {hasFilters(filters) ? (
            <Button
              variant="secondary"
              size="small"
              onClick={() => {
                onApply({});
              }}
            >
              Clear filters
            </Button>
          ) : null}
          <Button variant="primary" size="small" type="submit">
            Apply filters
          </Button>
        </div>
      </div>
    </form>
  );
}
