// The one lifecycle mapping (D-08): versionBadges for a version, headBadges for a list row's head.
// A draft head is never shown as Older (Pitfall 3); Deprecated is an extra badge after the primary.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ContractSummary } from "@/api/types";
import {
  headBadges,
  headVersionLike,
  VersionBadges,
  versionBadges,
} from "./versionStatus";

type Head = Pick<ContractSummary, "version" | "latest_version">;

function head(
  state: string,
  options: { latest?: boolean; deprecated?: boolean } = {},
): Head {
  return {
    version: { number: 3, state, deprecated: options.deprecated ?? false },
    latest_version: options.latest
      ? { version_id: "v-1.2.0", semver: "1.2.0" }
      : null,
  };
}

const labels = (row: Head) => headBadges(row).map((badge) => badge.label);

describe("headBadges", () => {
  it("shows Draft for a draft head with no Latest", () => {
    expect(labels(head("draft"))).toEqual(["Draft"]);
  });

  it("never shows a draft as Older, even when the contract has a Latest", () => {
    expect(labels(head("draft", { latest: true }))).toEqual(["Draft"]);
    expect(labels(head("draft"))).not.toContain("Older");
  });

  it("shows Latest for a published head when the contract has a Latest", () => {
    expect(labels(head("published", { latest: true }))).toEqual(["Latest"]);
  });

  it("shows Preview for a published head without a Latest", () => {
    expect(labels(head("published"))).toEqual(["Preview"]);
  });

  it("shows Unpublished for an unpublished head", () => {
    expect(labels(head("unpublished"))).toEqual(["Unpublished"]);
    expect(labels(head("unpublished", { latest: true }))).toEqual([
      "Unpublished",
    ]);
  });

  it("adds Deprecated after the primary badge", () => {
    expect(
      labels(head("published", { latest: true, deprecated: true })),
    ).toEqual(["Latest", "Deprecated"]);
    expect(labels(head("unpublished", { deprecated: true }))).toEqual([
      "Unpublished",
      "Deprecated",
    ]);
    expect(labels(head("draft", { deprecated: true }))).toEqual([
      "Draft",
      "Deprecated",
    ]);
  });

  it("carries no sunset date, so the label is plain Deprecated", () => {
    expect(
      headVersionLike(head("published", { deprecated: true })).sunset_date,
    ).toBeNull();
  });

  it("gives each primary state its own intent and icon", () => {
    const primaries = [
      head("draft"),
      head("published", { latest: true }),
      head("published"),
      head("unpublished"),
    ].map((row) => headBadges(row)[0]);
    expect(primaries.map((badge) => badge?.key)).toEqual([
      "draft",
      "latest",
      "preview",
      "unpublished",
    ]);
    expect(new Set(primaries.map((badge) => badge?.icon)).size).toBe(4);
    expect(primaries[0]?.intent).toBe("neutral");
  });
});

describe("versionBadges (unchanged for a ContractVersion)", () => {
  const base = {
    deprecated: false,
    sunset_date: null,
    lifecycle: "published" as const,
  };

  it("keeps latest, preview, older and unpublished", () => {
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
      versionBadges({ ...base, lifecycle: "unpublished", status: "older" }).map(
        (b) => b.label,
      ),
    ).toEqual(["Unpublished"]);
  });

  it("keeps the sunset date in the Deprecated label", () => {
    expect(
      versionBadges({
        ...base,
        status: "latest",
        deprecated: true,
        sunset_date: "2027-03-01",
      }).map((b) => b.label),
    ).toEqual(["Latest", "Deprecated · sunset 2027-03-01"]);
  });

  it("treats a draft with no status as Draft", () => {
    expect(
      versionBadges({ ...base, lifecycle: "draft", status: null }).map(
        (b) => b.label,
      ),
    ).toEqual(["Draft"]);
  });
});

describe("VersionBadges", () => {
  it("renders each badge as text", () => {
    render(
      <VersionBadges
        version={{
          lifecycle: "published",
          status: "latest",
          deprecated: true,
          sunset_date: null,
        }}
      />,
    );
    expect(screen.getByText("Latest")).toBeInTheDocument();
    expect(screen.getByText("Deprecated")).toBeInTheDocument();
  });
});
