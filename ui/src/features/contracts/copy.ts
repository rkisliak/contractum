// Strings of the contract screens that more than one component shows (one module, so an i18n
// pass has one place).

/** The info notice on the Overview of a contract the caller may see the metadata of only (D-12). */
export const metadataOnlyNotice =
  "You can see this contract's metadata only. Its content and versions are not available at your access level.";

/** Shown in place of a fact the API redacted for a metadata-only caller (null there means hidden, not empty). */
export const redactedFact = "Not available at your access level";

/** "status: draft" or "no status", for a document's ODCS `status` (null: the document sets none). */
function statusPhrase(status: string | null): string {
  return status === null ? "no status" : `status: ${status}`;
}

/**
 * The publish dialog's warning when the saved document's ODCS `status` is not `active`. A
 * released version (no prerelease part) cannot be published with it; a prerelease (a Preview) can,
 * after an explicit acknowledgement.
 */
export const publishStatusCopy = {
  releasedTitle: "A released version needs status: active",
  says: (status: string | null): string =>
    status === null
      ? "This document sets no status."
      : `This document says status: ${status}.`,
  releasedFix: (canChange: boolean): string =>
    canChange
      ? "Change it to active, save the draft, then publish."
      : "Set status: active in the document, save the draft, then publish.",
  previewTitle: (status: string | null): string =>
    status === null
      ? "This document sets no status"
      : `This document says status: ${status}`,
  previewRisk: (status: string | null): string =>
    `Consumers will get a Preview with ${statusPhrase(status)}.`,
  acknowledge: (status: string | null): string =>
    `Publish with ${statusPhrase(status)} anyway`,
  changeToActive: "Change status to active",
  changed:
    "Status changed to active. Review the change, save the draft, then publish.",
};

/** The Versions notice after a publish whose document's ODCS `status` was not `active`. */
export const publishedStatusCopy = {
  says: (status: string): string =>
    status === "none"
      ? "Its document sets no status."
      : status === "other"
        ? "Its document does not say status: active."
        : `Its document says status: ${status}.`,
  fix: "To correct it, publish a new version that says status: active.",
};

/** The header badge beside "Latest x.y.z" when the Latest document's ODCS `status` is not `active`. */
export function latestStatusBadge(status: string | null): string {
  return status === null ? "No status" : `Status: ${status}`;
}
