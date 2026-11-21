// /governance/dry-run?rule_set_id=&version=&folder_id= (D-UI-11-13): what would happen to the
// published contracts in scope if a rule-set version (or the draft) applied to them now.
import { useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { DryRunForm, type DryRunChoice } from "./DryRunForm";
import { DryRunResults, type DryRunRequest } from "./DryRunResults";
import styles from "./gov.module.css";

export type DryRunSearch = {
  rule_set_id?: string;
  version?: number | "draft";
  folder_id?: string;
};

export function DryRunPage({
  search,
  onSearchChange,
}: {
  search: DryRunSearch;
  onSearchChange: (next: DryRunSearch) => void;
}) {
  const fromSearch: DryRunRequest | null =
    search.rule_set_id && search.version !== undefined
      ? {
          ruleSetId: search.rule_set_id,
          version: search.version,
          folderId: search.folder_id,
          run: 0,
        }
      : null;
  const [request, setRequest] = useState<DryRunRequest | null>(fromSearch);

  function run(choice: DryRunChoice) {
    setRequest((previous) => ({
      ruleSetId: choice.ruleSetId,
      version: choice.version,
      folderId: choice.folderId,
      run: (previous?.run ?? 0) + 1,
    }));
    onSearchChange({
      rule_set_id: choice.ruleSetId,
      version: choice.version,
      folder_id: choice.folderId,
    });
  }

  return (
    <>
      <PageHeader
        title="Dry runs"
        description="See which published contracts would fail a rule-set version before you publish or enforce it. Nothing is changed."
        breadcrumbs={[{ label: "Governance" }]}
      />
      <div className={styles.stack}>
        <DryRunForm
          initial={{
            folderId: search.folder_id,
            ruleSetId: search.rule_set_id,
            version: search.version,
          }}
          onRun={run}
        />
        {request === null ? (
          <p className={styles.muted}>
            Choose a scope folder, a rule set and a version, then run the
            dry-run.
          </p>
        ) : (
          <DryRunResults request={request} />
        )}
      </div>
    </>
  );
}
