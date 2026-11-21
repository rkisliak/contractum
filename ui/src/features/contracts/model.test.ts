import { describe, expect, it } from "vitest";
import type { Problem } from "@/api/problem";
import { changeTitle, groupChanges } from "./ContractDiff";
import { breakingCopy, semverConflictCopy } from "./DraftEditor";
import { blankSkeleton, suggestOdcsId } from "./NewContractPage";
import { declaredBump } from "./PublishDialog";
import {
  compatibilityOf,
  flattenReport,
  structuralOnly,
  summaryOf,
} from "./report";
import { versionBadges } from "./versionStatus";

const report = {
  status: "completed",
  structural: {
    valid: false,
    error_count: 1,
    errors: [
      {
        pointer: "/version",
        keyword: "type",
        message: "The value has the wrong type.",
      },
    ],
  },
  findings: [
    {
      rule_set: { id: null, name: null, version: null },
      source: "native",
      mode: "enforce",
      rule_key: "semver-breaking-change",
      severity: "error",
      outcome: "fail",
      message:
        "A breaking change needs a new major version. The smallest passing version is 2.0.0.",
      location: { pointer: "/version", path: "$.version" },
      blocking: true,
    },
    {
      rule_set: { id: "rs", name: "acme-baseline", version: 1 },
      source: "mandatory",
      mode: "warn",
      rule_key: "require-description",
      severity: "error",
      outcome: "fail",
      message: "Add description.purpose.",
      location: { pointer: "", path: "$" },
      blocking: false,
    },
    {
      rule_set: { id: "rs2", name: "naming", version: 3 },
      source: "explicit",
      mode: "enforce",
      rule_key: "snake",
      severity: "info",
      outcome: "fail",
      message: "Prefer snake case.",
      location: null,
      blocking: false,
    },
  ],
  summary: { publishable: false, blocking_count: 2 },
};

describe("flattenReport", () => {
  it("groups by the server's blocking flag and labels sources", () => {
    const findings = flattenReport(report);
    expect(findings.map((finding) => [finding.group, finding.source])).toEqual([
      ["blocking", "ODCS schema"],
      ["blocking", "Platform check"],
      ["warning", "acme-baseline v1"],
      ["info", "naming v3"],
    ]);
    expect(findings[3]?.pointer).toBeNull();
    expect(summaryOf(report)).toEqual({
      publishable: false,
      blockingCount: 2,
      status: "completed",
    });
    expect(flattenReport(null)).toEqual([]);
  });
});

describe("versionBadges", () => {
  it("covers every status, with deprecation orthogonal", () => {
    const base = {
      deprecated: false,
      sunset_date: null,
      lifecycle: "published" as const,
    };
    expect(
      versionBadges({ ...base, status: "latest" }).map((b) => b.label),
    ).toEqual(["Latest"]);
    expect(
      versionBadges({ ...base, status: "preview" }).map((b) => b.label),
    ).toEqual(["Preview"]);
    expect(
      versionBadges({ ...base, status: "older" }).map((b) => b.label),
    ).toEqual(["Older"]);
    expect(
      versionBadges({
        ...base,
        status: "latest",
        deprecated: true,
        sunset_date: "2027-03-01",
      }).map((b) => b.label),
    ).toEqual(["Latest", "Deprecated · sunset 2027-03-01"]);
    expect(
      versionBadges({ ...base, lifecycle: "unpublished", status: "older" }).map(
        (b) => b.label,
      ),
    ).toEqual(["Unpublished"]);
  });
});

describe("refusal copy", () => {
  it("names the declared and required versions", () => {
    expect(
      breakingCopy("The smallest passing version is 2.0.0.", "1.1.0"),
    ).toBe(
      "Breaking changes need a new major version. Declared 1.1.0; required at least 2.0.0.",
    );
    const problem: Problem = {
      kind: "problem",
      status: 409,
      slug: "semver-conflict",
      title: "Conflict",
      detail: null,
      errors: [],
      requestId: null,
      extensions: { highest_semver: "1.4.0" },
    };
    expect(semverConflictCopy(problem, "1.2.0")).toBe(
      "Version 1.2.0 already exists or is below the highest (1.4.0). Choose a higher version.",
    );
  });

  it("computes the declared bump", () => {
    expect(declaredBump("1.0.0", "2.0.0")).toBe("major");
    expect(declaredBump("1.0.0", "1.1.0")).toBe("minor");
    expect(declaredBump("1.0.0", "1.0.1")).toBe("patch");
    expect(declaredBump(null, "1.0.0")).toBeNull();
  });
});

describe("diff grouping", () => {
  it("orders Breaking, Warning, Info", () => {
    const change = (klass: string, code: string) => ({
      code,
      class: klass,
      path: "p",
      attribute: null,
      before: null,
      after: null,
      pointer_before: null,
      pointer_after: null,
    });
    const groups = groupChanges([
      change("info", "property-added"),
      change("breaking", "property-removed"),
    ]);
    expect(groups.map((group) => [group.key, group.changes.length])).toEqual([
      ["breaking", 1],
      ["warning", 0],
      ["info", 1],
    ]);
    expect(changeTitle(change("info", "property-added"))).toBe(
      "Property added",
    );
  });
});

describe("new contract helpers", () => {
  it("suggests an ODCS id from the folder path and name and builds a skeleton", () => {
    expect(
      suggestOdcsId(
        {
          path: "acme/payments/ledger",
        } as never,
        "Settled Payments",
      ),
    ).toBe("urn:acme:payments:ledger:settled-payments");
    expect(blankSkeleton("yaml", "urn:x", "X")).toContain("kind: DataContract");
    expect(JSON.parse(blankSkeleton("json", "urn:x", "X"))).toMatchObject({
      id: "urn:x",
      apiVersion: "v3.2.0",
    });
  });

  it("starts a blank document with the platform default ODCS version, v3.2.0 without one", () => {
    expect(blankSkeleton("yaml", "urn:x", "X", "v3.1.0")).toContain(
      "apiVersion: v3.1.0\n",
    );
    expect(
      JSON.parse(blankSkeleton("json", "urn:x", "X", "v3.1.0")),
    ).toMatchObject({ apiVersion: "v3.1.0" });
    expect(blankSkeleton("yaml", "urn:x", "X")).toContain(
      "apiVersion: v3.2.0\n",
    );
  });
});

describe("report readers on partial or foreign shapes", () => {
  it("reads a summary only from an object", () => {
    expect(summaryOf("not a report")).toEqual({
      publishable: null,
      blockingCount: 0,
      status: null,
    });
    expect(summaryOf({ status: 5, summary: [] })).toEqual({
      publishable: null,
      blockingCount: 0,
      status: null,
    });
  });

  it("labels severities and fills missing messages and sources", () => {
    const findings = flattenReport({
      structural: { errors: ["not a record", { pointer: "/id" }] },
      findings: [
        null,
        { severity: "warn", outcome: "error", rule_set: { name: "x" } },
        { severity: "warn", blocking: false },
        { severity: "critical", rule_set: "nope", location: "nope" },
        {},
      ],
    });
    expect(
      findings.map((finding) => [
        finding.severity,
        finding.source,
        finding.message,
      ]),
    ).toEqual([
      ["Error", "ODCS schema", "The document does not match the ODCS schema."],
      ["Rule error", "x", ""],
      ["Warning", "Rule set", ""],
      ["critical", "Rule set", ""],
      ["Finding", "Rule set", ""],
    ]);
    expect(findings.map((finding) => finding.id)).toEqual([
      "s-1",
      "f-1",
      "f-2",
      "f-3",
      "f-4",
    ]);
    expect(findings[1]?.group).toBe("warning");
  });

  it("reads structural errors of a save response alone", () => {
    expect(
      structuralOnly({ errors: [{ pointer: "/kind", message: "Bad kind." }] }),
    ).toMatchObject([
      { group: "blocking", pointer: "/kind", message: "Bad kind." },
    ]);
    expect(structuralOnly(undefined)).toEqual([]);
  });

  it("reads the compatibility block and keeps only object changes", () => {
    expect(
      compatibilityOf({
        compatibility: {
          required_bump: "major",
          declared_version: "2.0.0",
          changes: [{ code: "x", class: "breaking" }, "noise", 3],
        },
      }),
    ).toEqual({
      requiredBump: "major",
      declared: "2.0.0",
      changes: [{ code: "x", class: "breaking" }],
    });
    const empty = { requiredBump: null, declared: null, changes: [] };
    expect(compatibilityOf({ compatibility: { changes: "x" } })).toEqual(empty);
    expect(compatibilityOf({ compatibility: [] })).toEqual(empty);
    expect(compatibilityOf(null)).toEqual(empty);
  });
});

describe("semver conflict copy without a highest version", () => {
  it("leaves the highest version out when the problem does not name it", () => {
    const expected =
      "Version 1.2.0 already exists or is below the highest. Choose a higher version.";
    expect(
      semverConflictCopy(
        {
          kind: "problem",
          status: 409,
          slug: "semver-conflict",
          title: "Conflict",
          detail: null,
          errors: [],
          requestId: null,
        },
        "1.2.0",
      ),
    ).toBe(expected);
    expect(
      semverConflictCopy(
        { kind: "network", status: 0, requestId: null },
        "1.2.0",
      ),
    ).toBe(expected);
  });
});
