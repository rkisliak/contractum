// Direct unit tests of the audit search-param module: every branch of the validator, the API
// query builder, the time conversions, presets and hasFilters.
import { describe, expect, it } from "vitest";
import {
  AGGREGATE_TYPES,
  CATEGORIES,
  FILTER_KEYS,
  PRESETS,
  SERVICES,
  fromLocalInput,
  hasFilters,
  presetRange,
  toApiQuery,
  toLocalInput,
  toUtc,
  validateAuditSearch,
} from "./query";

describe("validateAuditSearch", () => {
  it("keeps every valid key, trimmed", () => {
    expect(
      validateAuditSearch({
        aggregate_type: " contract ",
        aggregate_id: "c-1",
        actor_id: "local:u1",
        service: "contracts",
        action: "contract.version.published",
        category: "access",
        from: "2026-11-15T09:00:00Z",
        to: "2026-11-16T09:00:00Z",
        event: "e-9",
      }),
    ).toEqual({
      aggregate_type: "contract",
      aggregate_id: "c-1",
      actor_id: "local:u1",
      service: "contracts",
      action: "contract.version.published",
      category: "access",
      from: "2026-11-15T09:00:00Z",
      to: "2026-11-16T09:00:00Z",
      event: "e-9",
    });
  });

  it("drops non-strings, blanks, unknown categories and unparseable instants", () => {
    expect(
      validateAuditSearch({
        aggregate_type: 42,
        aggregate_id: "   ",
        actor_id: null,
        service: ["identity"],
        action: undefined,
        category: "CHANGE",
        from: "yesterday",
        to: "   ",
        event: { id: "e" },
      }),
    ).toEqual({});
  });

  it("caps each field at its maximum length", () => {
    const long = "x".repeat(500);
    const result = validateAuditSearch({
      aggregate_type: long,
      aggregate_id: long,
      actor_id: long,
      service: long,
      action: long,
      event: long,
    });
    expect(result.aggregate_type).toHaveLength(64);
    expect(result.aggregate_id).toHaveLength(200);
    expect(result.actor_id).toHaveLength(200);
    expect(result.service).toHaveLength(64);
    expect(result.action).toHaveLength(128);
    expect(result.event).toHaveLength(64);
  });

  it("accepts every declared category", () => {
    for (const category of CATEGORIES) {
      expect(validateAuditSearch({ category })).toEqual({ category });
    }
  });
});

describe("toApiQuery", () => {
  it("returns an empty query without filters", () => {
    expect(toApiQuery({})).toEqual({});
  });

  it("skips undefined and empty values and normalises both times to UTC", () => {
    expect(
      toApiQuery({
        aggregate_type: "card",
        aggregate_id: "",
        service: "identity",
        action: "card.",
        category: "denial",
        from: "2026-11-15T10:00:00+01:00",
        to: "2026-11-15T12:00:00+01:00",
      }),
    ).toEqual({
      aggregate_type: "card",
      service: "identity",
      action: "card.",
      category: "denial",
      from: "2026-11-15T09:00:00.000Z",
      to: "2026-11-15T11:00:00.000Z",
    });
  });
});

describe("toUtc", () => {
  it("normalises a parseable instant", () => {
    expect(toUtc("2026-11-15T09:30:00-02:00")).toBe("2026-11-15T11:30:00.000Z");
  });

  it("returns an unparseable value unchanged", () => {
    expect(toUtc("not-a-time")).toBe("not-a-time");
  });
});

describe("toLocalInput and fromLocalInput", () => {
  it("is empty for undefined and invalid values", () => {
    expect(toLocalInput(undefined)).toBe("");
    expect(toLocalInput("garbage")).toBe("");
  });

  it("formats the local wall clock with zero padding", () => {
    const local = new Date(2026, 0, 5, 7, 3);
    expect(toLocalInput(local.toISOString())).toBe("2026-01-05T07:03");
  });

  it("converts a local value to UTC and back", () => {
    const utc = fromLocalInput("2026-03-04T05:06");
    expect(utc).toBe(new Date(2026, 2, 4, 5, 6).toISOString());
    expect(toLocalInput(utc)).toBe("2026-03-04T05:06");
  });

  it("is undefined for an empty or invalid local value", () => {
    expect(fromLocalInput("")).toBeUndefined();
    expect(fromLocalInput("2026-99-99T99:99")).toBeUndefined();
  });
});

describe("presetRange", () => {
  it("ends now with no upper bound", () => {
    const now = Date.parse("2026-11-15T10:00:00Z");
    expect(presetRange(86_400_000, now)).toEqual({
      from: "2026-11-14T10:00:00.000Z",
      to: undefined,
    });
  });

  it("defaults to the current time", () => {
    const before = Date.now();
    const range = presetRange(60_000);
    const from = Date.parse(range.from ?? "");
    expect(from).toBeGreaterThanOrEqual(before - 60_000);
    expect(from).toBeLessThanOrEqual(Date.now() - 60_000);
  });

  it("offers four presets of increasing length", () => {
    expect(PRESETS.map((preset) => preset.id)).toEqual([
      "1h",
      "24h",
      "7d",
      "30d",
    ]);
    const lengths = PRESETS.map((preset) => preset.ms);
    expect([...lengths].sort((a, b) => a - b)).toEqual(lengths);
  });
});

describe("hasFilters", () => {
  it("is false without any filter and ignores the event key", () => {
    expect(hasFilters({})).toBe(false);
    expect(hasFilters({ event: "e-1" } as never)).toBe(false);
  });

  it("is true for each filter key", () => {
    for (const key of FILTER_KEYS) {
      expect(hasFilters({ [key]: "x" })).toBe(true);
    }
  });
});

describe("catalogues", () => {
  it("lists the services and known aggregate types", () => {
    expect(SERVICES).toEqual(["identity", "contracts", "validation", "audit"]);
    expect(AGGREGATE_TYPES).toContain("contract");
    expect(AGGREGATE_TYPES).toContain("template");
  });
});
