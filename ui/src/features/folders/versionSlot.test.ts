// The version slot and the accessible name of a contract row in the folder tree (D-10).
import { describe, expect, it } from "vitest";
import type { ContractSummary } from "@/api/types";
import { makeContract } from "@/test/contracts-fixtures";
import { contractRowLabel, contractVersionSlot } from "./versionSlot";

function row(
  state: string,
  options: {
    semver?: string;
    deprecated?: boolean;
    name?: string | null;
    odcsId?: string;
  } = {},
): ContractSummary {
  return makeContract({
    name: options.name === undefined ? "Orders" : options.name,
    odcs_id: options.odcsId ?? "urn:x",
    version: { number: 3, state, deprecated: options.deprecated ?? false },
    latest_version: options.semver
      ? { version_id: `v-${options.semver}`, semver: options.semver }
      : null,
  });
}

describe("contractVersionSlot", () => {
  it("shows the Latest semver, not muted", () => {
    expect(contractVersionSlot(row("published", { semver: "1.2.0" }))).toEqual({
      text: "1.2.0",
      muted: false,
    });
  });

  it("keeps the Latest semver on a draft head of a published contract", () => {
    expect(contractVersionSlot(row("draft", { semver: "1.2.0" }))).toEqual({
      text: "1.2.0",
      muted: false,
    });
  });

  it("says Not published, muted, for a draft or unpublished head with no Latest", () => {
    const expected = { text: "Not published", muted: true };
    expect(contractVersionSlot(row("draft"))).toEqual(expected);
    expect(contractVersionSlot(row("unpublished"))).toEqual(expected);
  });

  it("says No Latest version, muted, for a published head with no Latest", () => {
    expect(contractVersionSlot(row("published"))).toEqual({
      text: "No Latest version",
      muted: true,
    });
  });
});

describe("contractRowLabel", () => {
  it("names a deprecated Latest with its badges and version", () => {
    expect(
      contractRowLabel(row("published", { semver: "1.2.0", deprecated: true })),
    ).toBe("Orders, Latest, Deprecated, version 1.2.0");
  });

  it("falls back to the odcs_id for an empty name and says not published for a draft", () => {
    expect(contractRowLabel(row("draft", { name: "", odcsId: "urn:x" }))).toBe(
      "urn:x, Draft, not published",
    );
  });

  it("falls back to the odcs_id for a missing name", () => {
    expect(
      contractRowLabel(row("unpublished", { name: null, odcsId: "urn:y" })),
    ).toBe("urn:y, Unpublished, not published");
  });

  it("says not published for a Preview without a Latest", () => {
    expect(contractRowLabel(row("published"))).toBe(
      "Orders, Preview, not published",
    );
  });
});
