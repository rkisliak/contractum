// The rule form's editable values and their conversion to and from the API's RuleDef. Fixtures
// are edited as JSON arrays of fixture objects ({ document, description?, ctx?, previous?,
// refs? }); parsing JSON is the only processing done in the browser (no rule evaluation).
import type { RuleDef } from "@/api/types";

export type RuleFormValues = {
  key: string;
  description: string;
  target: string;
  condition: string;
  severity: RuleDef["severity"];
  message: string;
  match: NonNullable<RuleDef["match"]>;
  on_empty: NonNullable<RuleDef["on_empty"]>;
  pass: string;
  fail: string;
};

export const RULE_KEY_PATTERN = /^[a-z][a-z0-9-]{0,62}$/;

export const SEVERITIES: RuleDef["severity"][] = ["error", "warning", "info"];
export const MATCH_MODES: NonNullable<RuleDef["match"]>[] = ["each", "any"];
export const ON_EMPTY: NonNullable<RuleDef["on_empty"]>[] = ["pass", "fail"];

/** Drops the null keys the API echoes back, so fixtures read as the author wrote them. */
function tidy(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(tidy);
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value).filter(([, entry]) => entry !== null),
    );
  }
  return value;
}

export function formFromRule(rule: RuleDef): RuleFormValues {
  return {
    key: rule.key,
    description: rule.description ?? "",
    target: rule.target,
    condition: rule.condition,
    severity: rule.severity,
    message: rule.message,
    match: rule.match,
    on_empty: rule.on_empty,
    pass: JSON.stringify(tidy(rule.fixtures.pass), null, 2),
    fail: JSON.stringify(tidy(rule.fixtures.fail), null, 2),
  };
}

export function emptyForm(): RuleFormValues {
  return {
    key: "",
    description: "",
    target: "$",
    condition: "",
    severity: "error",
    message: "",
    match: "each",
    on_empty: "pass",
    pass: '[\n  { "document": {} }\n]',
    fail: '[\n  { "document": {} }\n]',
  };
}

export type FormErrors = Partial<Record<keyof RuleFormValues, string>>;

function parseFixtures(
  text: string,
  kind: "pass" | "fail",
): { value: RuleDef["fixtures"]["pass"] } | { error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text === "" ? "[]" : text);
  } catch {
    return { error: `The ${kind} fixtures are not valid JSON.` };
  }
  if (!Array.isArray(parsed))
    return { error: `The ${kind} fixtures must be a JSON array.` };
  for (const item of parsed) {
    if (typeof item !== "object" || item === null || !("document" in item))
      return {
        error: `Each ${kind} fixture is an object with a "document" field.`,
      };
  }
  return { value: parsed as RuleDef["fixtures"]["pass"] };
}

/** Validates the form locally (shape only) and builds the RuleDef to send. */
export function ruleFromForm(
  values: RuleFormValues,
): { rule: RuleDef } | { errors: FormErrors } {
  const errors: FormErrors = {};
  if (!RULE_KEY_PATTERN.test(values.key))
    errors.key =
      "Start with a lowercase letter; use lowercase letters, digits and hyphens (up to 63).";
  if (values.condition.trim() === "")
    errors.condition = "Enter a CEL condition.";
  if (values.message.trim() === "")
    errors.message = "Enter the message authors see.";
  if (values.target.trim() === "")
    errors.target = "Enter a JSONPath target, for example $.";
  const pass = parseFixtures(values.pass, "pass");
  const fail = parseFixtures(values.fail, "fail");
  if ("error" in pass) errors.pass = pass.error;
  if ("error" in fail) errors.fail = fail.error;
  if (Object.keys(errors).length > 0 || "error" in pass || "error" in fail)
    return { errors };
  return {
    rule: {
      key: values.key,
      description:
        values.description.trim() === "" ? null : values.description.trim(),
      target: values.target.trim(),
      condition: values.condition,
      severity: values.severity,
      message: values.message.trim(),
      match: values.match,
      on_empty: values.on_empty,
      fixtures: { pass: pass.value, fail: fail.value },
    },
  };
}
