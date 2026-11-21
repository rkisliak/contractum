// The server check report (POST /rule-sets/{id}/draft/check, and the advisory report on every
// draft save) mapped back to rules and fixtures (D-UI-11-08). The OpenAPI document types the
// report as a free object, so its shape is read defensively here: anything unexpected is shown
// as a rule-set problem, never dropped. Nothing user-authored is ever evaluated in the browser.

export type CheckProblem = {
  rule_key?: string | null;
  code: string;
  detail?: string | null;
  fixture_kind?: "pass" | "fail" | null;
  fixture_index?: number | null;
  error_code?: string | null;
};

export type RuleCheck = {
  rule_key: string;
  ok: boolean;
  problems: CheckProblem[];
};

export type CheckReport =
  | {
      status: "ok";
      ok: boolean;
      ruleSetProblems: CheckProblem[];
      rules: RuleCheck[];
    }
  | { status: "unavailable" };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toProblem(value: unknown): CheckProblem {
  if (!isRecord(value)) return { code: "unknown" };
  return {
    code: typeof value.code === "string" ? value.code : "unknown",
    rule_key: typeof value.rule_key === "string" ? value.rule_key : null,
    detail: typeof value.detail === "string" ? value.detail : null,
    fixture_kind:
      value.fixture_kind === "pass" || value.fixture_kind === "fail"
        ? value.fixture_kind
        : null,
    fixture_index:
      typeof value.fixture_index === "number" ? value.fixture_index : null,
    error_code: typeof value.error_code === "string" ? value.error_code : null,
  };
}

/** Parses the `check` field of a draft answer; null when no check ran. */
export function parseCheck(raw: unknown): CheckReport | null {
  if (!isRecord(raw)) return null;
  if (raw.status === "unavailable") return { status: "unavailable" };
  const rules = Array.isArray(raw.rules)
    ? raw.rules.filter(isRecord).map((rule) => ({
        rule_key: typeof rule.rule_key === "string" ? rule.rule_key : "",
        ok: rule.ok === true,
        problems: Array.isArray(rule.problems)
          ? rule.problems.map(toProblem)
          : [],
      }))
    : [];
  const ruleSetProblems = Array.isArray(raw.rule_set_problems)
    ? raw.rule_set_problems.map(toProblem)
    : [];
  return { status: "ok", ok: raw.ok === true, ruleSetProblems, rules };
}

export type RuleStatus = "ok" | "problems" | "unchecked";

/** Per-rule status for the rule list icons. */
export function ruleStatuses(
  report: CheckReport | null,
): Map<string, RuleCheck> {
  const map = new Map<string, RuleCheck>();
  if (report?.status !== "ok") return map;
  for (const rule of report.rules) map.set(rule.rule_key, rule);
  return map;
}

export function statusOf(report: CheckReport | null, key: string): RuleStatus {
  const rule = ruleStatuses(report).get(key);
  if (rule === undefined) return "unchecked";
  return rule.ok ? "ok" : "problems";
}

const FIELD_CODES: Record<string, "condition" | "target"> = {
  compile_error: "condition",
  not_boolean: "condition",
  expression_too_large: "condition",
  ast_too_large: "condition",
  target_invalid: "target",
  target_too_long: "target",
};

/** The form field a problem belongs to, or "fixtures" / "rule" for the rest. */
export function fieldOf(
  problem: CheckProblem,
): "condition" | "target" | "fixtures" | "rule" {
  if (problem.fixture_kind) return "fixtures";
  if (problem.code.includes("fixture")) return "fixtures";
  return FIELD_CODES[problem.code] ?? "rule";
}

const CODE_LABELS: Record<string, string> = {
  compile_error: "The condition does not compile",
  not_boolean: "The condition does not return true or false",
  target_invalid: "The target is not a valid JSONPath",
  target_too_long: "The target is too long",
  expression_too_large: "The condition is too large",
  ast_too_large: "The condition is too complex",
  missing_pass_fixture: "Add at least one pass fixture",
  missing_fail_fixture: "Add at least one fail fixture",
  too_many_fixtures: "Too many fixtures",
  fixture_too_large: "A fixture is too large",
  fixture_changes_unavailable: "The fixture's changes could not be derived",
  scale_probe_failed: "The rule does not scale to large contracts",
  empty_rule_set: "The rule set needs at least one rule",
  too_many_rules: "The rule set has too many rules",
  duplicate_rule_key: "Two rules share a key",
};

/** One readable line for a problem ("Pass fixture 2: expected pass, got fail"). */
export function describeProblem(problem: CheckProblem): string {
  if (problem.code === "fixture_misbehaved" && problem.fixture_kind) {
    const kind = problem.fixture_kind === "pass" ? "Pass" : "Fail";
    const n = (problem.fixture_index ?? 0) + 1;
    const got = /evaluated to (\w+)/.exec(problem.detail ?? "")?.[1];
    const outcome = got
      ? `expected ${problem.fixture_kind}, got ${got}`
      : (problem.detail ?? "it did not behave as expected");
    const code = problem.error_code ? ` (${problem.error_code})` : "";
    return `${kind} fixture ${String(n)}: ${outcome}${code}`;
  }
  const label = CODE_LABELS[problem.code] ?? problem.code.replace(/_/g, " ");
  if (
    problem.fixture_kind &&
    problem.fixture_index !== null &&
    problem.fixture_index !== undefined
  ) {
    const kind = problem.fixture_kind === "pass" ? "Pass" : "Fail";
    return `${kind} fixture ${String(problem.fixture_index + 1)}: ${label}`;
  }
  return label;
}
