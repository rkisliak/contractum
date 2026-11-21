// Every permission in the registry snapshot (dc_auth roles.py, kept in sync by
// tests/repo/test_ui_permission_registry.py) has a readable label, a tooltip, an area and a
// capability; every role has a label and a meaning; and the matrix and sentences read right.
import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  ACCESS_AREAS,
  AREA_COPY,
  CAPABILITIES,
  PERMISSION_LABELS,
  hasFullAccess,
  partialNote,
  permissionLabel,
  summarizeAccess,
} from "./permissionLabels";
import { PERMISSION_IDS, ROLE_IDS } from "./permissionRegistry";
import { roleLabel, roleMeaning } from "./roles";

describe("permission labels", () => {
  it("labels every permission in the registry, and nothing else", () => {
    for (const id of PERMISSION_IDS) {
      const entry = PERMISSION_LABELS[id];
      expect(entry, id).toBeDefined();
      expect(entry.label.trim(), id).not.toBe("");
      expect(entry.label, id).not.toContain(":");
      expect(entry.tooltip.trim().endsWith("."), id).toBe(true);
      expect(ACCESS_AREAS, id).toContain(entry.area);
      expect(CAPABILITIES, id).toContain(entry.capability);
    }
    expect(Object.keys(PERMISSION_LABELS).sort()).toEqual(
      [...PERMISSION_IDS].sort(),
    );
  });

  it("gives every capability column used by an area a sentence verb", () => {
    for (const id of PERMISSION_IDS) {
      const { area, capability } = PERMISSION_LABELS[id];
      expect(AREA_COPY[area].verbs[capability], id).toBeTruthy();
    }
  });

  it("labels and explains every role", () => {
    for (const role of ROLE_IDS) {
      expect(roleLabel(role), role).not.toContain("_");
      expect(roleMeaning(role), role).not.toBe("");
    }
  });

  it("returns undefined for an unknown id", () => {
    expect(permissionLabel("thing:do")).toBeUndefined();
    expect(permissionLabel("contract:create")?.label).toBe("Create contracts");
  });
});

describe("summarizeAccess", () => {
  const teamEditor = [
    "folder:read",
    "ruleset:read-published",
    "contract:read",
    "contract:read-metadata",
    "contract:read-published",
    "contract:view",
    "contract:validate",
    "contract:create",
    "contract:update",
    "contract:publish",
    "contract:delete",
  ];

  it("fills the contract matrix with yes, partly and n/a cells", () => {
    const { areas } = summarizeAccess(teamEditor, "here");
    const contracts = areas.find((area) => area.area === "contracts");
    const state = Object.fromEntries(
      (contracts?.cells ?? []).map((cell) => [cell.capability, cell.state]),
    );
    expect(state).toEqual({
      view: "yes",
      create: "yes",
      edit: "yes",
      publish: "yes",
      archive: "partly",
      administer: "n/a",
    });
    expect(contracts?.sentence).toBe(
      "You can view, create, edit, publish and delete or restore contracts here. Not included: restore deleted contracts.",
    );
  });

  it("says plainly when an area is out of reach", () => {
    const { areas } = summarizeAccess(teamEditor, "here");
    const audit = areas.find((area) => area.area === "audit");
    expect(audit?.held).toEqual([]);
    expect(audit?.sentence).toBe("You cannot work with the audit log here.");
  });

  it("keeps ids the registry does not know", () => {
    expect(summarizeAccess(["thing:do"], "here").unknown).toEqual(["thing:do"]);
  });
});

describe("hasFullAccess and partialNote", () => {
  it("is full only when every area is fully held", () => {
    expect(hasFullAccess(summarizeAccess([...PERMISSION_IDS], "here"))).toBe(
      true,
    );
    expect(hasFullAccess(summarizeAccess(["contract:read"], "here"))).toBe(
      false,
    );
  });

  it("names a partly held column's gaps, at most two", () => {
    const contracts = summarizeAccess(
      [
        "contract:list",
        "contract:view",
        "contract:read-metadata",
        "contract:read-published",
        "contract:read",
        "contract:validate",
        "contract:delete",
      ],
      "here",
    ).areas.find((area) => area.area === "contracts");
    if (!contracts) throw new Error("no contracts area");
    expect(partialNote(contracts)).toBe("Can't restore deleted contracts.");
    const people = summarizeAccess(["grant:read"], "here").areas.find(
      (area) => area.area === "people",
    );
    if (!people) throw new Error("no people area");
    expect(partialNote(people)).toMatch(/^Can't .+, .+ and \d+ more\.$/);
  });
});

// Pins every label, tooltip, area, capability and area sentence byte for byte, so the table can be
// restructured (grouped by area and capability) without a value changing unnoticed.
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`);
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value);
}

describe("the label table content", () => {
  it("is exactly the pinned content", () => {
    const digest = createHash("sha256")
      .update(canonical({ PERMISSION_LABELS, AREA_COPY }))
      .digest("hex");
    expect(digest).toBe(
      "685adc7942d4e7a038cb559f5e2311db24e5ad02d38510cdab2b62843a92dc83",
    );
  });
});
