// Version slot and accessible name of a contract row in the folder tree (D-10), from the list row
// alone: the head's state and the contract's Latest version, never a per-contract request.
import type { ContractSummary } from "@/api/types";
import {
  headBadges,
  headVersionLike,
} from "@/features/contracts/versionStatus";
import { treeCopy } from "./copy";
import { contractDisplayName } from "./tree";

/** True when the head's lifecycle badge is Latest (a published head of a contract with a Latest). */
function headIsLatest(contract: ContractSummary): boolean {
  return headVersionLike(contract).status === "latest";
}

/**
 * The version after a contract row's lifecycle badge(s): the Latest semver ("[Latest] 1.2.0"), or
 * "Latest 1.2.0" when the head is not the Latest (a draft head of a published contract), so a Draft
 * badge is never followed by a semver that is not the draft's. Without a Latest it is a muted word:
 * a draft or an unpublished head says "Not published", a published head (a Preview) says "No
 * Latest version", so the slot is never empty.
 */
export function contractVersionSlot(contract: ContractSummary): {
  text: string;
  muted: boolean;
} {
  const latest = contract.latest_version;
  if (latest)
    return {
      text: headIsLatest(contract)
        ? latest.semver
        : treeCopy.latestVersion(latest.semver),
      muted: false,
    };
  if (contract.version.state === "published")
    return { text: treeCopy.noLatest, muted: true };
  return { text: treeCopy.notPublished, muted: true };
}

/** "No Latest version" to "no Latest version": only the first letter drops, "Latest" stays a name. */
function lowerFirst(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1);
}

/**
 * "{name}, {badge labels}, version {semver}", "..., Latest version {semver}" when the head is not
 * the Latest, "..., no Latest version" for a published head that has no Latest (a Preview), or
 * "..., not published" for a head that was never published or is unpublished. It matches what the
 * visible version slot says.
 */
export function contractRowLabel(contract: ContractSummary): string {
  const badges = headBadges(contract)
    .map((badge) => badge.label)
    .join(", ");
  const latest = contract.latest_version;
  const versionText = latest
    ? headIsLatest(contract)
      ? `version ${latest.semver}`
      : `Latest version ${latest.semver}`
    : lowerFirst(
        contract.version.state === "published"
          ? treeCopy.noLatest
          : treeCopy.notPublished,
      );
  return treeCopy.rowName(contractDisplayName(contract), badges, versionText);
}
