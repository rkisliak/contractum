// Pure governance logic: the check-report mapper and problem wording, the rule form conversion,
// the rollout status (server mode only, never the clock) and the impact filters.
import { describe, expect, it } from "vitest";
import type { DryRunItem, ImpactFailingRule } from "@/api/types";
import {
  filterDrift,
  filterDryRun,
  ruleKeysOf,
} from "@/features/governance/filters";
import { describeProblem, fieldOf, parseCheck, statusOf } from "./check";
import {
  formatUtcDate,
  platformNote,
  relativeDays,
  rolloutStatus,
  scopeLabel,
} from "./rollout";
import { emptyForm, formFromRule, ruleFromForm } from "./ruleValues";

describe("check report", () => {
  const raw = {
    ok: false,
    rule_set_problems: [],
    rules: [
      { rule_key: "a", ok: true, problems: [] },
      {
        rule_key: "b",
        ok: false,
        problems: [
          { rule_key: "b", code: "compile_error", detail: "Syntax error" },
          {
            rule_key: "b",
            code: "fixture_misbehaved",
            detail: "the pass fixture evaluated to fail",
            fixture_kind: "pass",
            fixture_index: 1,
          },
        ],
      },
    ],
  };

  it("maps rules to statuses", () => {
    const report = parseCheck(raw);
    expect(statusOf(report, "a")).toBe("ok");
    expect(statusOf(report, "b")).toBe("problems");
    expect(statusOf(report, "c")).toBe("unchecked");
    expect(statusOf(null, "a")).toBe("unchecked");
  });

  it("reads an unavailable sandbox and ignores a missing check", () => {
    expect(parseCheck({ status: "unavailable" })).toEqual({
      status: "unavailable",
    });
    expect(parseCheck(null)).toBeNull();
  });

  it("words fixture outcomes and routes problems to fields", () => {
    const report = parseCheck(raw);
    const problems =
      report?.status === "ok" ? (report.rules[1]?.problems ?? []) : [];
    expect(problems.map(describeProblem)).toEqual([
      "The condition does not compile",
      "Pass fixture 2: expected pass, got fail",
    ]);
    expect(problems.map(fieldOf)).toEqual(["condition", "fixtures"]);
    expect(fieldOf({ code: "target_invalid" })).toBe("target");
  });
});

describe("rule form", () => {
  it("round-trips a rule and drops echoed nulls from fixtures", () => {
    const form = formFromRule({
      key: "has-description",
      description: null,
      target: "$",
      condition: "has(this.description)",
      severity: "error",
      message: "Add a description",
      match: "each",
      on_empty: "pass",
      fixtures: {
        pass: [{ document: { description: {} }, ctx: null }],
        fail: [{ document: {} }],
      },
    });
    expect(form.pass).not.toContain("ctx");
    const built = ruleFromForm(form);
    expect("rule" in built && built.rule.fixtures.pass).toEqual([
      { document: { description: {} } },
    ]);
  });

  it("rejects a bad key and non-JSON fixtures", () => {
    const built = ruleFromForm({ ...emptyForm(), key: "Bad Key", pass: "{" });
    expect("errors" in built && Object.keys(built.errors).sort()).toEqual([
      "condition",
      "key",
      "message",
      "pass",
    ]);
  });
});

describe("rollout status", () => {
  it("shows the server mode of the newest version", () => {
    expect(rolloutStatus(false, [])).toEqual({
      label: "Not mandatory",
      intent: "neutral",
    });
    expect(
      rolloutStatus(true, [
        {
          version_no: 1,
          mode: "enforce",
          enforce_at: "2026-01-01T00:00:00Z",
          paused: false,
        },
        {
          version_no: 2,
          mode: "warn",
          enforce_at: "2026-12-01T00:00:00Z",
          paused: false,
        },
      ]).label,
    ).toBe("Warn until 1 Dec 2026");
    // A past enforce_at with mode "warn" stays "Warn": the clock never overrides the server.
    expect(
      rolloutStatus(true, [
        {
          version_no: 3,
          mode: "warn",
          enforce_at: "2020-01-01T00:00:00Z",
          paused: false,
        },
      ]).intent,
    ).toBe("warning");
    expect(
      rolloutStatus(true, [
        { version_no: 1, mode: "enforce", enforce_at: null, paused: false },
      ]).label,
    ).toBe("Enforced");
  });

  it("reads Paused for a paused mandatory set whatever its versions say", () => {
    expect(
      rolloutStatus(
        true,
        [{ version_no: 1, mode: "warn", enforce_at: null, paused: true }],
        true,
      ),
    ).toEqual({ label: "Paused", intent: "warning" });
    expect(rolloutStatus(false, [], true).label).toBe("Not mandatory");
    // an absent paused property is false
    expect(
      rolloutStatus(true, [
        { version_no: 1, mode: "enforce", enforce_at: null, paused: false },
      ]).label,
    ).toBe("Enforced");
  });

  it("explains a platform set: paused, still warning, or nothing", () => {
    const version = {
      version_no: 1,
      content_sha256: "x",
      rule_keys: [],
      deprecated_at: null,
      enforce_at: "2026-12-01T00:00:00Z",
      published_at: "2026-11-15T09:00:00Z",
      published_by: "system",
    };
    const warning = [
      {
        version_no: 1,
        mode: "warn" as const,
        enforce_at: version.enforce_at,
        paused: false,
      },
    ];
    expect(
      platformNote({
        isPlatform: true,
        resolved: warning,
        versions: [version],
        paused: true,
        pausedAt: "2026-11-18T10:00:00Z",
      }),
    ).toBe(
      "Paused on 18 Nov 2026: its rules keep running and report findings, but they do not block publishing until resumed.",
    );
    expect(
      platformNote({
        isPlatform: true,
        resolved: warning,
        versions: [version],
      }),
    ).toBe(
      "Added by a platform update on 15 Nov 2026; warns until 1 Dec 2026 so existing contracts can be fixed, then blocks publishing.",
    );
    expect(
      platformNote({
        isPlatform: true,
        resolved: [
          { version_no: 1, mode: "enforce", enforce_at: null, paused: false },
        ],
        versions: [version],
      }),
    ).toBeNull();
    expect(
      platformNote({
        isPlatform: false,
        resolved: warning,
        versions: [version],
        paused: true,
      }),
    ).toBeNull();
  });

  it("words relative days", () => {
    const now = new Date("2026-11-15T12:00:00Z");
    expect(relativeDays("2026-11-24T12:00:00Z", now)).toBe("in 9 days");
    expect(relativeDays("2026-11-15T12:00:00Z", now)).toBe("today");
    expect(relativeDays("2026-11-12T12:00:00Z", now)).toBe("3 days ago");
  });
});

describe("impact filters", () => {
  const rule = (key: string): ImpactFailingRule => ({
    rule_key: key,
    rule_set_id: "rs",
    version_no: 1,
    severity: "error",
    mode: "enforce",
    outcome: "fail",
    blocking: true,
    error_code: null,
    message: "m",
    pointers: [],
    pointers_total: 0,
    pointers_withheld: false,
  });
  const item = (
    id: string,
    folder: string,
    block: boolean,
    keys: string[],
  ): DryRunItem => ({
    contract_id: id,
    folder_id: folder,
    name: id,
    odcs_id: id,
    latest_semver: "1.0.0",
    status: "evaluated",
    would_block: block,
    counts: { fail: keys.length, error: 0, warn: 0 },
    failing_rules: keys.map(rule),
  });
  const items = [
    item("a", "f1", true, ["x"]),
    item("b", "f2", false, ["y"]),
    item("c", "f1", false, []),
  ];

  it("filters by would-block, rule key and folder", () => {
    expect(
      filterDryRun(items, { wouldBlock: true }).map((i) => i.contract_id),
    ).toEqual(["a"]);
    expect(
      filterDryRun(items, { ruleKey: "y" }).map((i) => i.contract_id),
    ).toEqual(["b"]);
    expect(
      filterDryRun(items, { folderId: "f1" }).map((i) => i.contract_id),
    ).toEqual(["a", "c"]);
    expect(ruleKeysOf(items)).toEqual(["x", "y"]);
  });

  it("filters drift by status", () => {
    const drift = items.map((i, n) => ({
      ...i,
      status: n === 0 ? ("drifted" as const) : ("at_risk" as const),
      pinned_rule_sets: [],
      current_versions: [],
    }));
    expect(filterDrift(drift, { status: "drifted" })).toHaveLength(1);
    expect(filterDrift(drift, {})).toHaveLength(3);
  });
});

describe("check report, defensive reading", () => {
  it("reads unexpected shapes as unknown problems instead of dropping them", () => {
    expect(
      parseCheck({
        ok: true,
        rules: [
          { rule_key: 7, ok: "yes", problems: "none" },
          "not a rule",
          { rule_key: "k", ok: false, problems: [42, { code: 3 }] },
        ],
        rule_set_problems: [{ code: "too_many_rules", error_code: "E1" }],
      }),
    ).toEqual({
      status: "ok",
      ok: true,
      rules: [
        { rule_key: "", ok: false, problems: [] },
        {
          rule_key: "k",
          ok: false,
          problems: [
            { code: "unknown" },
            {
              code: "unknown",
              rule_key: null,
              detail: null,
              fixture_kind: null,
              fixture_index: null,
              error_code: null,
            },
          ],
        },
      ],
      ruleSetProblems: [
        {
          code: "too_many_rules",
          rule_key: null,
          detail: null,
          fixture_kind: null,
          fixture_index: null,
          error_code: "E1",
        },
      ],
    });
  });

  it("treats missing lists as empty", () => {
    expect(parseCheck({ ok: "true", rules: {}, rule_set_problems: 1 })).toEqual(
      { status: "ok", ok: false, ruleSetProblems: [], rules: [] },
    );
  });

  it("routes fixture codes to the fixtures and unknown codes to the rule", () => {
    expect(fieldOf({ code: "missing_fail_fixture" })).toBe("fixtures");
    expect(fieldOf({ code: "duplicate_rule_key" })).toBe("rule");
    expect(fieldOf({ code: "compile_error", fixture_kind: "fail" })).toBe(
      "fixtures",
    );
  });

  it("words every kind of fixture outcome", () => {
    expect(
      describeProblem({
        code: "fixture_misbehaved",
        fixture_kind: "fail",
        fixture_index: null,
        detail: null,
        error_code: "NOT_FOUND",
      }),
    ).toBe("Fail fixture 1: it did not behave as expected (NOT_FOUND)");
    expect(
      describeProblem({
        code: "fixture_misbehaved",
        fixture_kind: "pass",
        fixture_index: 0,
        detail: "raised an error",
      }),
    ).toBe("Pass fixture 1: raised an error");
    expect(
      describeProblem({
        code: "fixture_too_large",
        fixture_kind: "fail",
        fixture_index: 2,
      }),
    ).toBe("Fail fixture 3: A fixture is too large");
    expect(
      describeProblem({
        code: "fixture_too_large",
        fixture_kind: "pass",
        fixture_index: 0,
      }),
    ).toBe("Pass fixture 1: A fixture is too large");
    expect(
      describeProblem({ code: "fixture_too_large", fixture_kind: "pass" }),
    ).toBe("A fixture is too large");
    expect(
      describeProblem({
        code: "fixture_too_large",
        fixture_kind: "pass",
        fixture_index: null,
      }),
    ).toBe("A fixture is too large");
    expect(describeProblem({ code: "brand_new_check" })).toBe(
      "brand new check",
    );
  });
});

describe("rollout wording edge cases", () => {
  it("keeps an unparseable date as written", () => {
    expect(formatUtcDate("soon")).toBe("soon");
    expect(relativeDays("soon")).toBe("");
  });

  it("says tomorrow and yesterday", () => {
    const now = new Date("2026-11-15T12:00:00Z");
    expect(relativeDays("2026-11-16T12:00:00Z", now)).toBe("tomorrow");
    expect(relativeDays("2026-11-14T12:00:00Z", now)).toBe("yesterday");
  });

  it("reads a mandatory set with nothing resolved and a warning without a date", () => {
    expect(rolloutStatus(true, undefined)).toEqual({
      label: "Mandatory, nothing published",
      intent: "neutral",
    });
    expect(
      rolloutStatus(true, [
        { version_no: 1, mode: "warn", enforce_at: null, paused: false },
      ]),
    ).toEqual({ label: "Warning", intent: "warning" });
  });

  it("explains a platform set only when the newest version is a dated warning", () => {
    const version = (n: number) => ({
      version_no: n,
      content_sha256: `x${String(n)}`,
      rule_keys: [],
      deprecated_at: null,
      enforce_at: null,
      published_at: "2026-11-15T09:00:00Z",
      published_by: "system",
    });
    expect(
      platformNote({
        isPlatform: true,
        resolved: undefined,
        versions: [],
        paused: true,
        pausedAt: null,
      }),
    ).toBe(
      "Paused: its rules keep running and report findings, but they do not block publishing until resumed.",
    );
    expect(
      platformNote({ isPlatform: true, resolved: undefined, versions: [] }),
    ).toBeNull();
    const warn = {
      version_no: 2,
      mode: "warn" as const,
      enforce_at: "2026-12-01T00:00:00Z",
      paused: false,
    };
    const warnings = [
      {
        version_no: 1,
        mode: "enforce" as const,
        enforce_at: null,
        paused: false,
      },
      warn,
    ];
    // The newest version is not among the loaded versions: no publish date to cite.
    expect(
      platformNote({
        isPlatform: true,
        resolved: warnings,
        versions: [version(1)],
      }),
    ).toBeNull();
    expect(
      platformNote({
        isPlatform: true,
        resolved: [{ ...warn, enforce_at: null }],
        versions: [version(2)],
      }),
    ).toBeNull();
    expect(
      platformNote({
        isPlatform: true,
        resolved: warnings,
        versions: [version(1), version(2)],
      }),
    ).toBe(
      "Added by a platform update on 15 Nov 2026; warns until 1 Dec 2026 so existing contracts can be fixed, then blocks publishing.",
    );
  });

  it("labels every scope and folder kind", () => {
    expect(scopeLabel("platform", null)).toBe("Platform");
    expect(scopeLabel("folder", "organization")).toBe("Organization");
    expect(scopeLabel("folder", "project")).toBe("Project");
    expect(scopeLabel("folder", "team")).toBe("Team");
    expect(scopeLabel("folder", "root")).toBe("Root");
    expect(scopeLabel("folder", null)).toBe("folder");
  });
});

describe("rule form validation", () => {
  const valid = {
    ...emptyForm(),
    key: "has-owner",
    condition: "has(this.owner)",
    message: "Name an owner.",
  };

  it("builds a rule with a trimmed description and treats empty fixtures as none", () => {
    const built = ruleFromForm({
      ...valid,
      description: "  Owner required  ",
      target: " $.schema ",
      pass: "",
    });
    expect("rule" in built && built.rule).toMatchObject({
      description: "Owner required",
      target: "$.schema",
      fixtures: { pass: [], fail: [{ document: {} }] },
    });
  });

  it("explains each fixture shape problem and an empty target", () => {
    const errorsOf = (changes: Partial<typeof valid>) => {
      const built = ruleFromForm({ ...valid, ...changes });
      return "errors" in built ? built.errors : {};
    };
    expect(errorsOf({ target: " ", fail: '{"document": {}}' })).toEqual({
      target: "Enter a JSONPath target, for example $.",
      fail: "The fail fixtures must be a JSON array.",
    });
    expect(errorsOf({ pass: "[1]" })).toEqual({
      pass: 'Each pass fixture is an object with a "document" field.',
    });
    expect(errorsOf({ fail: "[null]" })).toEqual({
      fail: 'Each fail fixture is an object with a "document" field.',
    });
    expect(errorsOf({ fail: '[{"ctx": {}}]' })).toEqual({
      fail: 'Each fail fixture is an object with a "document" field.',
    });
  });
});
