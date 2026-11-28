// Overview tab: metadata facts, the origin (template or another contract), and the delete and
// restore actions (D-UI-09-07, D-UI-09-18). A delete refused by a reference or a pin is explained
// with the referrers or the pinning cards. After a delete the contract is invisible everywhere, so
// the page moves to the contract list, which offers restore to callers who hold contract:restore.
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { Trash2 } from "lucide-react";
import { type ReactNode, useState } from "react";
import { isApiError, type Problem } from "@/api/problem";
import type { ContractSummary } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { orNotSet } from "@/components/DataTable";
import { ProblemAlert } from "@/components/ProblemAlert";
import { useCapabilities } from "@/features/access/useCapabilities";
import { useFolderList } from "@/features/folders/api";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { contractKeys, useApi, useContract } from "./api";
import { contractAccess, contractName } from "./ContractLayout";
import { metadataOnlyNotice, redactedFact } from "./copy";
import styles from "./Contracts.module.css";
import { PrincipalName } from "@/components/PrincipalName";

type Referrer = { contract_id: string; odcs_id: string };

function referrersOf(problem: Problem): { list: Referrer[]; hidden: number } {
  const ext = problem.kind === "problem" ? (problem.extensions ?? {}) : {};
  const list = Array.isArray(ext.referrers)
    ? ext.referrers.filter(
        (entry): entry is Referrer =>
          typeof entry === "object" &&
          entry !== null &&
          typeof (entry as Referrer).contract_id === "string" &&
          typeof (entry as Referrer).odcs_id === "string",
      )
    : [];
  const hidden =
    typeof ext.hidden_referrer_count === "number"
      ? ext.hidden_referrer_count
      : 0;
  return { list, hidden };
}

export function reasonsOf(problem: Problem): string[] {
  const ext = problem.kind === "problem" ? (problem.extensions ?? {}) : {};
  if (!Array.isArray(ext.reasons)) return [];
  return ext.reasons.map((reason) => {
    if (typeof reason === "string") return reason;
    if (typeof reason === "object" && reason !== null) {
      const record = reason as Record<string, unknown>;
      const name = record.card_name ?? record.name ?? record.card_id;
      return typeof name === "string" ? name : "A card";
    }
    return "A card";
  });
}

export function BlockedDelete({ problem }: { problem: Problem }) {
  if (problem.kind === "problem" && problem.slug === "contract-referenced") {
    const { list, hidden } = referrersOf(problem);
    return (
      <Alert intent="danger" title="This contract is still referenced">
        <p>Other published contracts link to it. Remove those links first.</p>
        <ul>
          {list.map((referrer) => (
            <li key={referrer.contract_id}>
              <Link
                to="/contracts/$contractId"
                params={{ contractId: referrer.contract_id }}
                className={styles.rowLink}
              >
                {referrer.odcs_id}
              </Link>
            </li>
          ))}
          {hidden > 0 ? <li>and {hidden} more you cannot see</li> : null}
        </ul>
      </Alert>
    );
  }
  if (problem.kind === "problem" && problem.slug === "contract-pinned") {
    const reasons = reasonsOf(problem);
    return (
      <Alert intent="danger" title="Subscriber cards pin this contract">
        <p>
          {problem.detail ?? "Cards actively pin a version of this contract."}
        </p>
        {reasons.length > 0 ? (
          <ul>
            {reasons.map((reason, index) => (
              <li key={`${reason}-${String(index)}`}>{reason}</li>
            ))}
          </ul>
        ) : null}
      </Alert>
    );
  }
  return <ProblemAlert problem={problem} context="action" />;
}

function Facts({
  contract,
  canSeeVersions,
}: {
  contract: ContractSummary;
  canSeeVersions: boolean;
}) {
  // A redacted field is null because the caller may not see it, not because it is empty.
  const hidden = (shown: ReactNode) =>
    contract.content_redacted ? (
      <span className={styles.muted}>{redactedFact}</span>
    ) : (
      shown
    );
  const folders = useFolderList(true).data?.folders ?? [];
  const folder = folders.find(
    (candidate) => candidate.id === contract.folder_id,
  );
  return (
    <dl className={styles.facts}>
      <dt>ODCS id</dt>
      <dd>
        <span className={styles.ident}>{contract.odcs_id}</span>
      </dd>
      <dt>Name</dt>
      <dd>{orNotSet(contract.name)}</dd>
      <dt>Folder</dt>
      <dd>
        <Link
          to="/folders/$folderId/contracts"
          params={{ folderId: contract.folder_id }}
          className={styles.rowLink}
        >
          {folder ? folder.path : contract.folder_id}
        </Link>
      </dd>
      <dt>Domain</dt>
      <dd>{hidden(orNotSet(contract.domain))}</dd>
      <dt>ODCS status</dt>
      <dd>
        {contract.status ? (
          <Badge
            intent={
              contract.latest_version && contract.status !== "active"
                ? "warning"
                : "neutral"
            }
          >
            {contract.status}
          </Badge>
        ) : (
          orNotSet(null)
        )}
      </dd>
      <dt>Tags</dt>
      <dd>
        {contract.content_redacted ? (
          hidden(null)
        ) : contract.tags && contract.tags.length > 0 ? (
          <span className={styles.badges}>
            {contract.tags.map((tag) => (
              <Badge key={tag} intent="neutral">
                {tag}
              </Badge>
            ))}
          </span>
        ) : (
          orNotSet(null)
        )}
      </dd>
      <dt>ODCS apiVersion</dt>
      <dd>{hidden(orNotSet(contract.api_version))}</dd>
      <dt>Latest version</dt>
      <dd>
        {contract.latest_version ? (
          canSeeVersions ? (
            <Link
              to="/contracts/$contractId/versions/$ref"
              params={{
                contractId: contract.id,
                ref: contract.latest_version.semver,
              }}
              className={styles.rowLink}
            >
              {contract.latest_version.semver}
            </Link>
          ) : (
            contract.latest_version.semver
          )
        ) : (
          "Not published"
        )}
      </dd>
      <dt>Origin</dt>
      <dd>
        {contract.template ? (
          <>
            Template{" "}
            <Link
              to="/templates/$templateId"
              params={{ templateId: contract.template.template_id }}
              className={styles.rowLink}
            >
              {contract.template.template_id.slice(0, 8)}
            </Link>{" "}
            version {contract.template.version_no}
          </>
        ) : contract.origin ? (
          <>
            Copied from{" "}
            <Link
              to="/contracts/$contractId"
              params={{ contractId: contract.origin.contract_id }}
              className={styles.rowLink}
            >
              another contract
            </Link>
          </>
        ) : (
          "Created from scratch"
        )}
      </dd>
      <dt>Created</dt>
      <dd>
        {formatDateTime(contract.created_at)} by{" "}
        <PrincipalName id={contract.created_by} />
      </dd>
      <dt>Updated</dt>
      <dd>
        {formatDateTime(contract.updated_at)} by{" "}
        <PrincipalName id={contract.updated_by} />
      </dd>
    </dl>
  );
}

export function ContractOverview({ contractId }: { contractId: string }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const contract = useContract(contractId);
  const caps = useCapabilities(contract.data?.folder_id ?? null);
  const [confirm, setConfirm] = useState(false);
  const [blocked, setBlocked] = useState<Problem | null>(null);
  const navigate = useNavigate();

  if (!contract.data) return null;
  const data = contract.data;
  const name = contractName(data);
  const { metadataOnly, canSeeVersions } = contractAccess(data, caps);
  const showActions =
    caps.can("contract:read") || canSeeVersions || caps.can("contract:delete");

  return (
    <div className={styles.stack}>
      {metadataOnly ? <Alert intent="info">{metadataOnlyNotice}</Alert> : null}
      {blocked ? <BlockedDelete problem={blocked} /> : null}
      <div className={styles.cards}>
        <Card title="Contract">
          <Facts contract={data} canSeeVersions={canSeeVersions} />
        </Card>
        {showActions ? (
          <Card title="Actions">
            <div className={styles.stackTight}>
              {canSeeVersions ? (
                <p className={styles.muted}>
                  {caps.can("contract:read")
                    ? "Drafts are edited on the Drafts tab; published versions and their lifecycle are on the Versions tab."
                    : "Published versions are on the Versions tab."}
                </p>
              ) : null}
              {caps.can("contract:read") || canSeeVersions ? (
                <div className={styles.toolbar}>
                  {caps.can("contract:read") ? (
                    <Link
                      to="/contracts/$contractId/drafts"
                      params={{ contractId: data.id }}
                      className={styles.rowLink}
                    >
                      Open drafts
                    </Link>
                  ) : null}
                  {canSeeVersions ? (
                    <Link
                      to="/contracts/$contractId/versions"
                      params={{ contractId: data.id }}
                      className={styles.rowLink}
                    >
                      View versions
                    </Link>
                  ) : null}
                </div>
              ) : null}
              {caps.can("contract:delete") ? (
                <div>
                  <Button
                    variant="outline-danger"
                    size="small"
                    icon={Trash2}
                    onClick={() => {
                      setBlocked(null);
                      setConfirm(true);
                    }}
                  >
                    Delete contract
                  </Button>
                </div>
              ) : null}
            </div>
          </Card>
        ) : null}
      </div>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={`Delete ${name}?`}
        body="It disappears from lists and fetches. An Organization admin can restore it."
        objectName={name}
        confirmLabel={`Delete ${name}`}
        typeToConfirm={name}
        busyLabel="Deleting…"
        onConfirm={async () => {
          try {
            await api.DELETE("/api/v1/contracts/contracts/{contract_id}", {
              params: { path: { contract_id: data.id } },
            });
          } catch (error) {
            if (
              isApiError(error) &&
              error.problem.kind === "problem" &&
              (error.problem.slug === "contract-referenced" ||
                error.problem.slug === "contract-pinned")
            ) {
              setBlocked(error.problem);
              return;
            }
            throw error;
          }
          await queryClient.invalidateQueries({ queryKey: contractKeys.lists });
          await navigate({
            to: "/contracts",
            search: {
              deleted: data.id,
              deletedName: name,
              deletedFolder: data.folder_id,
            },
          });
          queryClient.removeQueries({ queryKey: contractKeys.all(data.id) });
        }}
      />
    </div>
  );
}
