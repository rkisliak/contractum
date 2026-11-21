// One version (D-UI-10-02, D-UI-10-03): Document (read-only, as stored or converted with
// Accept, downloads), Validation report (the stored gate run with its rule-set pins) and
// Metadata. A deprecated version shows the deprecation and sunset banner.
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useRef, useState } from "react";
import type { ContractVersion } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { CopyButton } from "@/components/CopyButton";
import { NotFound } from "@/components/NotFound";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { Tabs } from "@/components/Tabs";
import {
  CodeEditor,
  type CodeEditorHandle,
} from "@/features/editor/CodeEditor";
import { locatePointer } from "@/features/editor/pointerMap";
import { useCapabilities } from "@/features/access/useCapabilities";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { isApiError } from "@/api/problem";
import { isNotFound, problemOf } from "@/lib/unexpected";
import {
  absoluteUrl,
  contractKeys,
  fetchVersionDocument,
  formatOf,
  useApi,
  useContract,
  type DocFormat,
} from "./api";
import { DocumentDownloads, FormatToggle } from "./DocumentDownloads";
import { FindingsPanel } from "./FindingsPanel";
import { asRecord, flattenReport, summaryOf } from "./report";
import { VersionActions } from "./VersionActions";
import { VersionBadges } from "./versionStatus";
import styles from "./Contracts.module.css";
import { PrincipalName } from "@/components/PrincipalName";

function DocumentPanel({
  contractId,
  version,
  odcsId,
}: {
  contractId: string;
  version: ContractVersion;
  odcsId: string;
}) {
  const api = useApi();
  const [format, setFormat] = useState<DocFormat | "stored">("stored");
  const doc = useQuery({
    queryKey: [
      ...contractKeys.version(contractId, version.semver),
      "document",
      format,
    ],
    queryFn: () =>
      fetchVersionDocument(
        api,
        contractId,
        version.semver,
        format === "stored" ? undefined : format,
      ),
    retry: false,
  });
  return (
    <div className={styles.stack}>
      <div className={styles.toolbar}>
        <FormatToggle id="version-format" value={format} onChange={setFormat} />
        <span className={styles.spacer} />
        <DocumentDownloads
          odcsId={odcsId}
          label={version.semver}
          fetchDoc={(next) =>
            fetchVersionDocument(api, contractId, version.semver, next)
          }
        />
      </div>
      {doc.error ? (
        <ProblemAlert
          problem={problemOf(doc.error)}
          onRetry={() => void doc.refetch()}
        />
      ) : doc.data === undefined ? (
        <Skeleton label="document" rows={8} />
      ) : (
        <CodeEditor
          value={doc.data.text}
          language={formatOf(doc.data.mediaType)}
          readOnly
          label={`Version ${version.semver} document`}
          fit="fill"
        />
      )}
    </div>
  );
}

function ReportPanel({
  contractId,
  version,
}: {
  contractId: string;
  version: ContractVersion;
}) {
  const api = useApi();
  const editorRef = useRef<CodeEditorHandle | null>(null);
  const report = useQuery({
    queryKey: [...contractKeys.version(contractId, version.semver), "report"],
    queryFn: async () => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/validation-report",
        { params: { path: { contract_id: contractId, ref: version.semver } } },
      );
      return data ?? null;
    },
    retry: false,
  });
  const doc = useQuery({
    queryKey: [
      ...contractKeys.version(contractId, version.semver),
      "document",
      "stored",
    ],
    queryFn: () => fetchVersionDocument(api, contractId, version.semver),
    retry: false,
  });
  if (report.error) {
    const err = report.error;
    if (
      isApiError(err) &&
      err.problem.kind === "problem" &&
      err.problem.slug === "validation-report-not-recorded"
    )
      return (
        <Alert intent="info">
          No report recorded (published before the gate stored reports).
        </Alert>
      );
    return (
      <ProblemAlert
        problem={problemOf(err)}
        onRetry={() => void report.refetch()}
      />
    );
  }
  if (!report.data) return <Skeleton label="validation report" rows={4} />;
  const data = report.data;
  const summary = summaryOf(data.report);
  const findings = flattenReport(data.report);
  const language = formatOf(doc.data?.mediaType ?? version.media_type);
  const locate = doc.data
    ? (pointer: string) => locatePointer(doc.data.text, pointer, language)
    : undefined;
  return (
    <div className={styles.stack}>
      <dl className={styles.facts}>
        <dt>Result</dt>
        <dd>
          {summary.publishable === false ? (
            <Badge intent="danger">Not publishable</Badge>
          ) : (
            <Badge intent="success">Passed the gate</Badge>
          )}
        </dd>
        <dt>Validated</dt>
        <dd>{formatDateTime(data.validated_at)}</dd>
        <dt>Run</dt>
        <dd>
          <span className={styles.ident}>{data.run_id}</span>{" "}
          <CopyButton
            value={data.run_id}
            label="Copy run ID"
            copiedLabel="Run ID copied"
          />
        </dd>
        <dt>Engine</dt>
        <dd>{data.engine_version}</dd>
      </dl>
      <h2 className={styles.sectionHeading}>Rule sets applied</h2>
      <div className={styles.tableScroll}>
        <table className={styles.findingTable}>
          <thead>
            <tr>
              <th scope="col">Rule set</th>
              <th scope="col">Scope</th>
              <th scope="col">Version</th>
              <th scope="col">Source</th>
              <th scope="col">Mode</th>
            </tr>
          </thead>
          <tbody>
            {data.pins.map((pin, index) => {
              const record = asRecord(pin) ?? {};
              const field = (key: string) => {
                const value = record[key];
                return typeof value === "string" || typeof value === "number"
                  ? String(value)
                  : "";
              };
              const id = field("rule_set_id");
              return (
                <tr key={`${id}-${String(index)}`}>
                  <td>
                    {id ? (
                      <Link
                        to="/rule-sets/$ruleSetId"
                        params={{ ruleSetId: id }}
                        className={styles.rowLink}
                      >
                        {field("name") || id}
                      </Link>
                    ) : (
                      field("name")
                    )}
                    {record.deprecated_version === true ? (
                      <>
                        {" "}
                        <Badge intent="warning">Deprecated</Badge>
                      </>
                    ) : null}
                  </td>
                  <td>{field("scope")}</td>
                  <td>{field("version")}</td>
                  <td>{field("source")}</td>
                  <td>{record.mode === "warn" ? "warn" : "enforce"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <FindingsPanel
        heading="Findings at publish"
        findings={findings}
        locate={locate}
        onReveal={(range) => {
          editorRef.current?.revealLine(range.line, range.column);
        }}
      />
      {doc.data ? (
        <CodeEditor
          ref={editorRef}
          value={doc.data.text}
          language={language}
          readOnly
          fit="fill"
          label={`Version ${version.semver} document`}
        />
      ) : null}
    </div>
  );
}

function MetadataPanel({ version }: { version: ContractVersion }) {
  return (
    <dl className={styles.facts}>
      <dt>Version id</dt>
      <dd>
        <span className={styles.ident}>{version.version_id}</span>
      </dd>
      <dt>Status</dt>
      <dd>{version.status}</dd>
      <dt>Lifecycle</dt>
      <dd>{version.lifecycle}</dd>
      <dt>Published</dt>
      <dd>
        {version.published_at ? (
          <>
            {formatDateTime(version.published_at)} by{" "}
            <PrincipalName id={version.published_by} />
          </>
        ) : (
          "Not recorded"
        )}
      </dd>
      <dt>Deprecated</dt>
      <dd>
        {version.deprecated
          ? `Yes, since ${formatDateTime(version.deprecated_at)}; sunset ${version.sunset_date ?? "not set"}`
          : "No"}
      </dd>
      <dt>Fetch URL</dt>
      <dd>
        <code className={styles.mono}>{absoluteUrl(version.fetch_url)}</code>{" "}
        <CopyButton
          value={absoluteUrl(version.fetch_url)}
          label="Copy fetch URL"
          copiedLabel="Fetch URL copied"
        />
      </dd>
      <dt>ETag</dt>
      <dd>
        <code className={styles.mono}>{version.etag}</code>
      </dd>
      <dt>SHA-256</dt>
      <dd>
        <code className={styles.mono}>{version.content_sha256}</code>
      </dd>
      <dt>Media type</dt>
      <dd>{version.media_type}</dd>
      <dt>ODCS apiVersion</dt>
      <dd>{version.odcs_api_version}</dd>
      <dt>Size</dt>
      <dd>{version.size_bytes} bytes</dd>
      <dt>Schema</dt>
      <dd>
        <code className={styles.mono}>
          {version.schema_sha ?? "Not recorded"}
        </code>
      </dd>
    </dl>
  );
}

export function VersionDetail({
  contractId,
  versionRef,
}: {
  contractId: string;
  versionRef: string;
}) {
  const api = useApi();
  const contract = useContract(contractId);
  const caps = useCapabilities(contract.data?.folder_id ?? null);
  const [notice, setNotice] = useState<string | null>(null);
  const meta = useQuery({
    queryKey: [...contractKeys.version(contractId, versionRef), "metadata"],
    queryFn: async () => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/metadata",
        { params: { path: { contract_id: contractId, ref: versionRef } } },
      );
      if (!data) throw new Error("metadata had no body");
      return data;
    },
    retry: false,
  });
  if (meta.isError)
    return isNotFound(meta.error) ? (
      <NotFound
        nested
        back={{ label: "versions", to: `/contracts/${contractId}/versions` }}
      />
    ) : (
      <ProblemAlert
        problem={problemOf(meta.error)}
        onRetry={() => void meta.refetch()}
      />
    );
  if (!meta.data || !contract.data)
    return <Skeleton label="version" rows={6} />;
  const version = meta.data;
  return (
    <section className={styles.stack} aria-label={`Version ${version.semver}`}>
      <div className={styles.toolbar}>
        <h2 className={styles.sectionHeading}>
          Version <span className={styles.ident}>{version.semver}</span>
        </h2>
        <span className={styles.badges}>
          <VersionBadges version={version} />
        </span>
        <span className={styles.spacer} />
        <VersionActions
          contract={contract.data}
          version={version}
          latest={contract.data.latest_version?.semver ?? null}
          canPublish={caps.can("contract:publish")}
          onNotice={setNotice}
        />
      </div>
      <p className={styles.status} role="status">
        {notice}
      </p>
      {version.deprecated ? (
        <Alert intent="warning" title="Deprecated">
          Deprecated on {formatDateTime(version.deprecated_at)}, sunset on{" "}
          {version.sunset_date ?? "a date not set"}.
        </Alert>
      ) : null}
      <Tabs
        label="Version sections"
        items={[
          {
            value: "document",
            label: "Document",
            content: (
              <DocumentPanel
                contractId={contractId}
                version={version}
                odcsId={contract.data.odcs_id}
              />
            ),
          },
          {
            value: "report",
            label: "Validation report",
            content: <ReportPanel contractId={contractId} version={version} />,
          },
          {
            value: "metadata",
            label: "Metadata",
            content: <MetadataPanel version={version} />,
          },
        ]}
      />
    </section>
  );
}
