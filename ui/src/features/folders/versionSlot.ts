// Version slot and accessible name of a contract row in the folder tree (D-10), from the list row
// alone: the head's state and the contract's Latest version, never a per-contract request.
import type { ContractSummary } from "@/api/types";
import { headBadges } from "@/features/contracts/versionStatus";
import { treeCopy } from "./copy";
import { contractDisplayName } from "./tree";

/**
 * The Contracts column cell: the Latest semver, or a muted word when there is none. A draft or an
 * unpublished head with no Latest says "Not published"; a published head with no Latest (a Preview)
 * says "No Latest version", so the slot is never empty.
 */
export function contractVersionSlot(contract: ContractSummary): {
  text: string;
  muted: boolean;
} {
  if (contract.latest_version)
    return { text: contract.latest_version.semver, muted: false };
  if (contract.version.state === "published")
    return { text: treeCopy.noLatest, muted: true };
  return { text: treeCopy.notPublished, muted: true };
}

/** "{name}, {badge labels}, version {semver}" or "{name}, {badge labels}, not published". */
export function contractRowLabel(contract: ContractSummary): string {
  const badges = headBadges(contract)
    .map((badge) => badge.label)
    .join(", ");
  const versionText = contract.latest_version
    ? `version ${contract.latest_version.semver}`
    : treeCopy.notPublished.toLowerCase();
  return treeCopy.rowName(contractDisplayName(contract), badges, versionText);
}
