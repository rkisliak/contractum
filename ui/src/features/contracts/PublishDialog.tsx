// The pre-publish dialog (D-UI-10-04): the declared version, the current Latest and a one-line
// diff summary against it (fetched when the dialog opens). A declared bump below the required one
// is flagged but never blocks: the server gate decides. The confirm reads "Publish 1.3.0".
// An advisory warning shows when the saved document's ODCS `status` is not `active` (it mirrors the
// platform rule `published-status-active`, which only warns at publish); it never disables Publish.
import { useQuery } from "@tanstack/react-query";
import { parse } from "yaml";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { ProblemAlert } from "@/components/ProblemAlert";
import type { Problem } from "@/api/problem";
import { useApi } from "./api";
import styles from "./Contracts.module.css";

const RANK: Record<string, number> = { none: 0, patch: 1, minor: 2, major: 3 };

export function declaredBump(
  from: string | null | undefined,
  to: string | null | undefined,
): string | null {
  const parse = (value: string | null | undefined) =>
    value
      ? /^(\d+)\.(\d+)\.(\d+)/.exec(value)?.slice(1).map(Number)
      : undefined;
  const a = parse(from);
  const b = parse(to);
  if (!a || !b) return null;
  if ((b[0] ?? 0) > (a[0] ?? 0)) return "major";
  if ((b[1] ?? 0) > (a[1] ?? 0)) return "minor";
  if ((b[2] ?? 0) > (a[2] ?? 0)) return "patch";
  return "none";
}

/**
 * The ODCS `status` of a document as text. `undefined`: nothing to judge (no text, not parseable,
 * or not a mapping). `null`: the document sets no status. Otherwise the value as a string.
 */
export function documentStatus(
  text: string | null | undefined,
): string | null | undefined {
  if (!text?.trim()) return undefined;
  let root: unknown;
  try {
    root = parse(text);
  } catch {
    return undefined;
  }
  if (root === null || typeof root !== "object" || Array.isArray(root)) {
    return undefined;
  }
  const record = root as Record<string, unknown>;
  if (!("status" in record)) return null;
  const value = record.status;
  if (value === null || value === undefined) return null;
  return typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
    ? String(value)
    : JSON.stringify(value);
}

export function PublishDialog({
  open,
  onOpenChange,
  contractId,
  draftId,
  declared,
  latest,
  documentStatus: status,
  busy,
  problem,
  onPublish,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  contractId: string;
  draftId: string;
  declared: string | null;
  latest: string | null;
  /** `documentStatus(saved)`: undefined when unknown, null when the document sets none. */
  documentStatus: string | null | undefined;
  busy: boolean;
  problem: Problem | null;
  onPublish: () => void;
}) {
  const api = useApi();
  const diff = useQuery({
    queryKey: ["contract", contractId, "publish-diff", draftId],
    enabled: open && latest !== null,
    queryFn: async () => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}/diff",
        {
          params: {
            path: { contract_id: contractId },
            query: { from: "latest", to: draftId, limit: 1 },
          },
        },
      );
      return data ?? null;
    },
    retry: false,
  });
  const required = diff.data?.required_bump ?? null;
  const bump = declaredBump(latest, declared);
  const tooSmall =
    required !== null &&
    bump !== null &&
    (RANK[bump] ?? 0) < (RANK[required] ?? 0);
  const label = declared ? `Publish ${declared}` : "Publish draft";
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`${label}?`}
      description="The publish gate validates the saved draft against every mandatory and attached rule set. Nothing is published if it fails."
      footer={
        <>
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              onOpenChange(false);
            }}
          >
            Keep editing
          </Button>
          <Button
            variant="primary"
            size="small"
            busy={busy}
            busyLabel="Publishing…"
            onClick={onPublish}
          >
            {label}
          </Button>
        </>
      }
    >
      <div className={styles.stackTight}>
        <dl className={styles.facts}>
          <dt>Declared version</dt>
          <dd>
            <span className={styles.ident}>{declared ?? "Not set"}</span>
          </dd>
          <dt>Current Latest</dt>
          <dd>
            {latest ? (
              <span className={styles.ident}>{latest}</span>
            ) : (
              "None: this becomes the Latest"
            )}
          </dd>
          {latest ? (
            <>
              <dt>Changes</dt>
              <dd>
                {diff.isPending ? (
                  "Comparing…"
                ) : diff.data ? (
                  <span className={styles.badges}>
                    <Badge intent={required === "major" ? "danger" : "neutral"}>
                      Needs {required === "none" ? "no" : `a ${required ?? ""}`}{" "}
                      bump
                    </Badge>
                    <span>
                      {String(diff.data.counts.breaking ?? 0)} breaking,{" "}
                      {String(diff.data.counts.warning ?? 0)} warning,{" "}
                      {String(diff.data.counts.info ?? 0)} info
                    </span>
                  </span>
                ) : (
                  "Comparison unavailable"
                )}
              </dd>
            </>
          ) : null}
        </dl>
        {tooSmall ? (
          <Alert intent="warning">
            This change needs a {required} bump; you declared {bump}. The gate
            decides whether it can publish.
          </Alert>
        ) : null}
        {status !== undefined && status !== "active" ? (
          <Alert intent="warning">
            {status === null ? (
              <>
                This document sets no <code>status</code>.
              </>
            ) : (
              <>
                This document says <code>status: {status}</code>.
              </>
            )}{" "}
            Consumers usually expect <code>active</code> for a published
            version. It is a warning only and does not block publishing.
          </Alert>
        ) : null}
        {latest ? (
          <p className={styles.muted}>
            A new version above the Latest publishes as a Preview; promote it to
            make it the Latest.
          </p>
        ) : null}
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
      </div>
    </Dialog>
  );
}
