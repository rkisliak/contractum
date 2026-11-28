// Versions tab (D-UI-10-01, D-UI-10-16, D-UI-10-17): every version, highest SemVer first, with
// status badges (text plus icon), publisher and time, size, media type and ODCS apiVersion, and a
// row actions menu. After a publish the page shows "Published 1.3.0 as Preview" (a warning that
// names the document's ODCS status when it was not `active`). A short "How to fetch" panel
// explains the URLs and links to the API docs.
import { WhenBy } from "@/components/DataTable/WhenBy";
import { Link } from "@tanstack/react-router";
import { History } from "lucide-react";
import { useState } from "react";
import type { ContractVersion } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Card } from "@/components/Card";
import { DataTable, type Column } from "@/components/DataTable";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { TextLink } from "@/components/TextLink";
import { useCapabilities } from "@/features/access/useCapabilities";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { problemOf } from "@/lib/unexpected";
import { absoluteUrl, useContract, useContractVersions } from "./api";
import { VersionActions } from "./VersionActions";
import { VersionBadges } from "./versionStatus";
import { publishedStatusCopy } from "./copy";
import styles from "./Contracts.module.css";
import { PrincipalName } from "@/components/PrincipalName";

/**
 * The allow-list of the `documentStatus` search param: the published document's ODCS status when
 * it was not `active` ("none": it set no status, "other": any value outside the ODCS list).
 */
export const PUBLISHED_DOCUMENT_STATUSES = [
  "proposed",
  "draft",
  "deprecated",
  "retired",
  "none",
  "other",
] as const;
export type PublishedDocumentStatus =
  (typeof PUBLISHED_DOCUMENT_STATUSES)[number];

export function isPublishedDocumentStatus(
  value: unknown,
): value is PublishedDocumentStatus {
  return (PUBLISHED_DOCUMENT_STATUSES as readonly unknown[]).includes(value);
}

/**
 * The `documentStatus` search param for a publish of a document with this ODCS status
 * (`documentStatus(saved)`): nothing when it is `active` or unknown.
 */
export function publishedDocumentStatus(status: string | null | undefined): {
  documentStatus?: PublishedDocumentStatus;
} {
  if (status === undefined || status === "active") return {};
  if (status === null) return { documentStatus: "none" };
  return {
    documentStatus: isPublishedDocumentStatus(status) ? status : "other",
  };
}

export type VersionsSearch = {
  published?: string;
  status?: string;
  warnings?: number;
  documentStatus?: PublishedDocumentStatus;
};

function sizeLabel(bytes: number): string {
  return bytes < 1024
    ? `${String(bytes)} B`
    : `${(bytes / 1024).toFixed(1)} KiB`;
}

export function ContractVersions({
  contractId,
  search,
}: {
  contractId: string;
  search: VersionsSearch;
}) {
  const contract = useContract(contractId);
  const caps = useCapabilities(contract.data?.folder_id ?? null);
  const versions = useContractVersions(contractId);
  const [notice, setNotice] = useState<string | null>(null);
  const latest = versions.data?.find(
    (version) =>
      version.status === "latest" && version.lifecycle === "published",
  );
  const canPublish = caps.can("contract:publish");
  // Checked again here: the router hands down raw search keys of the parent routes as well.
  const documentStatus = isPublishedDocumentStatus(search.documentStatus)
    ? search.documentStatus
    : undefined;

  const columns: Column<ContractVersion>[] = [
    {
      id: "semver",
      header: "Version",
      rowHeader: true,
      cell: (row) => (
        <Link
          to="/contracts/$contractId/versions/$ref"
          params={{ contractId, ref: row.semver }}
          className={styles.rowLink}
        >
          <span className={styles.ident}>{row.semver}</span>
        </Link>
      ),
    },
    {
      id: "status",
      header: "Status",
      cell: (row) => (
        <span className={styles.badges}>
          <VersionBadges version={row} />
        </span>
      ),
    },
    {
      id: "published",
      header: "Published",
      cell: (row) =>
        row.published_at ? (
          <WhenBy
            when={formatDateTime(row.published_at)}
            by={<PrincipalName id={row.published_by} />}
          />
        ) : (
          "Not recorded"
        ),
    },
    {
      id: "size",
      header: "Size",
      align: "end",
      cell: (row) => sizeLabel(row.size_bytes),
    },
    {
      id: "media",
      header: "Format",
      cell: (row) => (row.media_type.includes("json") ? "JSON" : "YAML"),
    },
    { id: "api", header: "ODCS", cell: (row) => row.odcs_api_version },
    {
      id: "actions",
      header: "Actions",
      cell: (row) =>
        contract.data ? (
          <VersionActions
            contract={contract.data}
            version={row}
            latest={latest?.semver ?? null}
            canPublish={canPublish}
            onNotice={setNotice}
          />
        ) : null,
    },
  ];

  return (
    <section className={styles.stack} aria-label="Versions">
      {search.published ? (
        <Alert
          intent={documentStatus ? "warning" : "success"}
          title={`Published ${search.published} as ${search.status === "latest" ? "Latest" : search.status === "preview" ? "Preview" : "a new version"}${documentStatus ? `. ${publishedStatusCopy.says(documentStatus)}` : ""}`}
        >
          {search.status === "preview"
            ? "It is above the Latest, so consumers reach it by its own version number. Promote it to make it the Latest."
            : "Consumers fetching the Latest now receive it."}
          {documentStatus ? ` ${publishedStatusCopy.fix}` : null}
        </Alert>
      ) : null}
      <p className={styles.status} role="status">
        {notice}
      </p>
      {versions.error ? (
        <ProblemAlert
          problem={problemOf(versions.error)}
          onRetry={() => void versions.refetch()}
        />
      ) : versions.data === undefined ? (
        <Skeleton label="versions" rows={3} />
      ) : versions.data.length === 0 ? (
        <EmptyState
          icon={History}
          heading="No published versions"
          body="Publish a draft to create the first version. It becomes the Latest."
          action={
            <Link
              to="/contracts/$contractId/drafts"
              params={{ contractId }}
              className={styles.rowLink}
            >
              Open drafts
            </Link>
          }
        />
      ) : (
        <DataTable
          caption="Versions"
          captionHidden
          columns={columns}
          rows={versions.data}
          getRowId={(row) => row.version_id}
        />
      )}
      <Card title="How to fetch">
        <div className={styles.stackTight}>
          <p className={styles.muted}>
            The Latest is always at{" "}
            <code className={styles.mono}>
              {absoluteUrl(
                `/api/v1/contracts/contracts/${contractId}/versions/latest`,
              )}
            </code>
            . Any version, a Preview included, is at its own number, for example{" "}
            <code className={styles.mono}>
              …/versions/{latest?.semver ?? "1.0.0"}
            </code>
            . Send a bearer token and{" "}
            <code className={styles.mono}>Accept: application/yaml</code> or{" "}
            <code className={styles.mono}>application/json</code>.
          </p>
          <TextLink href="/api/v1/docs" external>
            API documentation
          </TextLink>
        </div>
      </Card>
    </section>
  );
}
