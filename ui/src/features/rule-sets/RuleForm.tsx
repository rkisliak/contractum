// The rule form (D-UI-11-07): key, description, JSONPath target, CEL condition, severity,
// message, match and on-empty, and the pass and fail fixtures side by side, like a unit test.
// Server check problems for the rule show under the field they concern (D-UI-11-08). The same
// form renders a published version read-only.
import { CircleCheck, CircleX } from "lucide-react";
import { SelectField, TextareaField, TextField } from "@/components/form";
import { describeProblem, fieldOf, type CheckProblem } from "./check";
import {
  MATCH_MODES,
  ON_EMPTY,
  SEVERITIES,
  type FormErrors,
  type RuleFormValues,
} from "./ruleValues";
import styles from "@/features/governance/gov.module.css";

function ProblemList({
  problems,
  label,
}: {
  problems: CheckProblem[];
  label: string;
}) {
  if (problems.length === 0) return null;
  return (
    <ul className={styles.problemList} aria-label={label}>
      {problems.map((problem, index) => (
        <li key={`${problem.code}-${String(index)}`} className={styles.problem}>
          <strong>{describeProblem(problem)}</strong>
          {problem.detail && problem.code !== "fixture_misbehaved" ? (
            <pre className={styles.problemDetail}>{problem.detail}</pre>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function fixtureCount(text: string): number {
  try {
    const parsed: unknown = JSON.parse(text);
    return Array.isArray(parsed) ? parsed.length : 0;
  } catch {
    return 0;
  }
}

function FixtureOutcome({
  kind,
  text,
  problems,
  checked,
}: {
  kind: "pass" | "fail";
  text: string;
  problems: CheckProblem[];
  checked: boolean;
}) {
  if (!checked) return null;
  const count = fixtureCount(text);
  const failing = new Set(
    problems
      .filter((problem) => problem.fixture_kind === kind)
      .map((problem) => problem.fixture_index ?? -1),
  );
  if (count === 0) return null;
  return (
    <ul className={styles.ruleMeta} aria-label={`${kind} fixture results`}>
      {Array.from({ length: count }, (_, index) =>
        failing.has(index) ? (
          <li key={index} className={styles.bad}>
            <CircleX size={14} aria-hidden="true" /> {kind} {index + 1} failed
          </li>
        ) : (
          <li key={index} className={styles.ok}>
            <CircleCheck size={14} aria-hidden="true" /> {kind} {index + 1} ok
          </li>
        ),
      )}
    </ul>
  );
}

export function RuleForm({
  values,
  onChange,
  errors = {},
  problems = [],
  checked = false,
  readOnly = false,
  keyLocked = false,
  idPrefix = "rule",
}: {
  values: RuleFormValues;
  onChange?: (values: RuleFormValues) => void;
  errors?: FormErrors;
  problems?: CheckProblem[];
  checked?: boolean;
  readOnly?: boolean;
  keyLocked?: boolean;
  idPrefix?: string;
}) {
  const set = <K extends keyof RuleFormValues>(
    key: K,
    value: RuleFormValues[K],
  ) => {
    onChange?.({ ...values, [key]: value });
  };
  const byField = (field: ReturnType<typeof fieldOf>) =>
    problems.filter((problem) => fieldOf(problem) === field);
  const id = (name: string) => `${idPrefix}-${name}`;

  return (
    <div className={styles.form}>
      <TextField
        id={id("key")}
        label="Key"
        helper={
          keyLocked
            ? "The key identifies the rule in reports; it cannot change once saved."
            : "Lowercase letters, digits and hyphens, for example require-description."
        }
        autoComplete="off"
        spellCheck={false}
        maxLength={63}
        value={values.key}
        error={errors.key}
        readOnly={readOnly || keyLocked}
        onChange={(event) => {
          set("key", event.target.value);
        }}
      />
      <TextField
        id={id("description")}
        label="Description"
        autoComplete="off"
        optional
        maxLength={1000}
        value={values.description}
        readOnly={readOnly}
        onChange={(event) => {
          set("description", event.target.value);
        }}
      />
      <TextField
        id={id("target")}
        label="Target (JSONPath)"
        helper="The nodes the condition runs on, as RFC 9535 JSONPath. $ is the whole contract."
        autoComplete="off"
        spellCheck={false}
        value={values.target}
        error={errors.target}
        readOnly={readOnly}
        onChange={(event) => {
          set("target", event.target.value);
        }}
      />
      <ProblemList problems={byField("target")} label="Target problems" />
      <TextareaField
        id={id("condition")}
        label="Condition (CEL)"
        helper="True when the node complies. Bindings: this, contract, previous, ctx. Guard optional fields with has(), for example has(this.description)."
        monospace
        rows={5}
        spellCheck={false}
        value={values.condition}
        error={errors.condition}
        readOnly={readOnly}
        placeholder="has(this.description) && this.description.purpose != ''"
        onChange={(event) => {
          set("condition", event.target.value);
        }}
      />
      <ProblemList problems={byField("condition")} label="Condition problems" />
      <div className={styles.formRow}>
        <SelectField
          id={id("severity")}
          label="Severity"
          value={values.severity}
          disabled={readOnly}
          options={SEVERITIES.map((value) => ({ value, label: value }))}
          onChange={(event) => {
            set("severity", event.target.value as RuleFormValues["severity"]);
          }}
        />
        <SelectField
          id={id("match")}
          label="Match"
          helper="each: every node must comply; any: one is enough."
          value={values.match}
          disabled={readOnly}
          options={MATCH_MODES.map((value) => ({ value, label: value }))}
          onChange={(event) => {
            set("match", event.target.value as RuleFormValues["match"]);
          }}
        />
        <SelectField
          id={id("on-empty")}
          label="If none match"
          helper="The outcome when the target selects nothing."
          value={values.on_empty}
          disabled={readOnly}
          options={ON_EMPTY.map((value) => ({ value, label: value }))}
          onChange={(event) => {
            set("on_empty", event.target.value as RuleFormValues["on_empty"]);
          }}
        />
      </div>
      <TextField
        id={id("message")}
        label="Message"
        autoComplete="off"
        helper="What a contract author sees when the rule fails."
        maxLength={500}
        value={values.message}
        error={errors.message}
        readOnly={readOnly}
        onChange={(event) => {
          set("message", event.target.value);
        }}
      />
      <div className={styles.fixtures}>
        <div className={styles.tight}>
          <TextareaField
            id={id("pass")}
            label="Pass fixtures (JSON)"
            helper='Documents the rule must accept: [{ "document": { … } }].'
            monospace
            rows={8}
            spellCheck={false}
            value={values.pass}
            error={errors.pass}
            readOnly={readOnly}
            onChange={(event) => {
              set("pass", event.target.value);
            }}
          />
          <FixtureOutcome
            kind="pass"
            text={values.pass}
            problems={problems}
            checked={checked}
          />
        </div>
        <div className={styles.tight}>
          <TextareaField
            id={id("fail")}
            label="Fail fixtures (JSON)"
            helper="Documents the rule must reject."
            monospace
            rows={8}
            spellCheck={false}
            value={values.fail}
            error={errors.fail}
            readOnly={readOnly}
            onChange={(event) => {
              set("fail", event.target.value);
            }}
          />
          <FixtureOutcome
            kind="fail"
            text={values.fail}
            problems={problems}
            checked={checked}
          />
        </div>
      </div>
      <ProblemList problems={byField("fixtures")} label="Fixture problems" />
      <ProblemList problems={byField("rule")} label="Rule problems" />
    </div>
  );
}
