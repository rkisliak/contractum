// The dry-run form (D-UI-11-13): a scope folder, a rule set that applies there (its own sets and
// the inherited folder sets; platform sets cannot be dry-run) and a version or the draft.
import { FlaskConical } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/Button";
import { SelectField } from "@/components/form";
import { FolderSelect } from "@/features/folders/FolderSelect";
import {
  useAvailableRuleSets,
  useFolderRuleSets,
} from "@/features/rule-sets/api";
import styles from "./gov.module.css";

export type DryRunChoice = {
  folderId: string;
  ruleSetId: string;
  version: number | "draft";
};

export function DryRunForm({
  initial,
  onRun,
}: {
  initial: Partial<DryRunChoice>;
  onRun: (choice: DryRunChoice) => void;
}) {
  const [folderId, setFolderId] = useState(initial.folderId ?? "");
  const [ruleSetId, setRuleSetId] = useState(initial.ruleSetId ?? "");
  const [version, setVersion] = useState(
    initial.version === undefined ? "" : String(initial.version),
  );
  const available = useAvailableRuleSets(folderId === "" ? null : folderId);
  const own = useFolderRuleSets(folderId);

  const options = new Map<
    string,
    { label: string; versions: number[]; draft: boolean }
  >();
  for (const set of available.data ?? []) {
    if (set.folder_id === null) continue;
    options.set(set.id, {
      label: set.name,
      versions: set.versions
        .filter((v) => v.deprecated_at === null)
        .map((v) => v.version_no)
        .sort((a, b) => b - a),
      draft: false,
    });
  }
  if (folderId !== "") {
    for (const set of own.data ?? []) {
      const known = options.get(set.id);
      options.set(set.id, {
        label: set.name,
        versions: known?.versions ?? [],
        draft: true,
      });
    }
  }
  const chosen = options.get(ruleSetId);
  const versionOptions = [
    ...(chosen?.versions ?? []).map((n) => ({
      value: String(n),
      label: `Version ${String(n)}`,
    })),
    ...(chosen?.draft
      ? [{ value: "draft", label: "Draft (unpublished)" }]
      : []),
  ];
  const ready = folderId !== "" && chosen !== undefined && version !== "";

  return (
    <form
      className={styles.toolbar}
      aria-label="Dry-run settings"
      onSubmit={(event) => {
        event.preventDefault();
        if (!ready) return;
        onRun({
          folderId,
          ruleSetId,
          version: version === "draft" ? "draft" : Number(version),
        });
      }}
    >
      <div className={styles.toolbarField}>
        <FolderSelect
          id="dry-run-folder"
          label="Scope folder"
          placeholder="Choose a folder"
          value={folderId}
          filter={(folder) => folder.kind !== "root"}
          onChange={(value) => {
            setFolderId(value);
            setRuleSetId("");
            setVersion("");
          }}
        />
      </div>
      <div className={styles.toolbarField}>
        <SelectField
          id="dry-run-rule-set"
          label="Rule set"
          controlSize="small"
          placeholder={
            folderId === "" ? "Choose a folder first" : "Choose a rule set"
          }
          disabled={folderId === ""}
          value={ruleSetId}
          options={[...options.entries()].map(([id, option]) => ({
            value: id,
            label: option.label,
          }))}
          onChange={(event) => {
            setRuleSetId(event.target.value);
            const next = options.get(event.target.value);
            const newest = next?.versions[0];
            setVersion(
              newest !== undefined
                ? String(newest)
                : next?.draft
                  ? "draft"
                  : "",
            );
          }}
        />
      </div>
      <div className={styles.toolbarField}>
        <SelectField
          id="dry-run-version"
          label="Version"
          controlSize="small"
          placeholder="Choose a version"
          disabled={chosen === undefined}
          value={version}
          options={versionOptions}
          onChange={(event) => {
            setVersion(event.target.value);
          }}
        />
      </div>
      <Button
        variant="primary"
        size="small"
        type="submit"
        icon={FlaskConical}
        disabled={!ready}
      >
        Run dry-run
      </Button>
    </form>
  );
}
