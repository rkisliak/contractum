// Document tab: the contract's current document (`GET .../document`) read-only, as stored or
// converted to YAML or JSON with Accept, plus downloads. The current document needs
// contract:read; a published-only reader (a consumer) is shown the Latest published version
// instead (`GET .../versions/latest`), so nothing it may not read is requested. The editor fills
// the window (it is the last element of the page) and "Expand editor" opens it full screen.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { CodeEditor } from "@/features/editor/CodeEditor";
import {
  EditorDialog,
  ExpandEditorButton,
} from "@/features/editor/EditorDialog";
import { problemOf } from "@/lib/unexpected";
import {
  contractKeys,
  fetchContractDocument,
  fetchVersionDocument,
  formatOf,
  useApi,
  useContract,
  type DocFormat,
} from "./api";
import { contractName } from "./ContractLayout";
import { DocumentDownloads, FormatToggle } from "./DocumentDownloads";
import styles from "./Contracts.module.css";

export function ContractDocument({ contractId }: { contractId: string }) {
  const api = useApi();
  const contract = useContract(contractId);
  const [format, setFormat] = useState<DocFormat | "stored">("stored");
  const [expanded, setExpanded] = useState(false);
  const caps = useCapabilities(contract.data?.folder_id ?? null, {
    enabled: contract.data !== undefined,
  });
  const publishedOnly = !caps.isPending && !caps.can("contract:read");
  const fetchDoc = (next?: DocFormat) =>
    publishedOnly
      ? fetchVersionDocument(api, contractId, "latest", next)
      : fetchContractDocument(api, contractId, next);
  const doc = useQuery({
    queryKey: [
      ...contractKeys.all(contractId),
      publishedOnly ? "latest-document" : "document",
      format,
    ],
    queryFn: () => fetchDoc(format === "stored" ? undefined : format),
    enabled: !caps.isPending,
    retry: false,
  });
  const odcsId = contract.data?.odcs_id ?? "contract";
  const label = contract.data?.latest_version?.semver ?? "current";
  const editor =
    doc.data === undefined
      ? null
      : {
          value: doc.data.text,
          language: formatOf(doc.data.mediaType),
          readOnly: true,
          label: "Contract document",
        };
  const problem = doc.error ? (
    <ProblemAlert
      problem={problemOf(doc.error)}
      onRetry={() => void doc.refetch()}
    />
  ) : null;
  return (
    <section className={styles.stack} aria-label="Document">
      <p className={styles.muted}>
        The contract&apos;s current document
        {contract.data?.latest_version
          ? ` (Latest ${contract.data.latest_version.semver}).`
          : " (a draft: nothing is published yet)."}
      </p>
      <div className={styles.toolbar}>
        <FormatToggle
          id="document-format"
          value={format}
          onChange={setFormat}
        />
        <span className={styles.spacer} />
        <DocumentDownloads
          odcsId={odcsId}
          label={label}
          fetchDoc={(next) => fetchDoc(next)}
        />
        {editor ? (
          <ExpandEditorButton
            onClick={() => {
              setExpanded(true);
            }}
          />
        ) : null}
      </div>
      {problem ??
        (editor === null ? (
          <Skeleton label="document" rows={8} />
        ) : expanded ? null : (
          <CodeEditor {...editor} fit="fill" />
        ))}
      <EditorDialog
        open={expanded}
        onOpenChange={setExpanded}
        title={`${contract.data ? contractName(contract.data) : "Contract"} · Document`}
        toolbar={
          <>
            <FormatToggle
              id="document-format-expanded"
              value={format}
              onChange={setFormat}
            />
            <span className={styles.spacer} />
            <DocumentDownloads
              odcsId={odcsId}
              label={label}
              fetchDoc={(next) => fetchDoc(next)}
            />
          </>
        }
        notices={problem}
        editor={editor}
      />
    </section>
  );
}
