// Pieces shared by the dry-run and drift reports: summary stats, the failing-rules cell (with
// "Details withheld" for metadata-only callers, D-UI-11-15) and the client-side filter bar.
import type { ImpactFailingRule } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Checkbox, SelectField } from "@/components/form";
import { useFolderPathLabel } from "@/features/folders/FolderSelect";
import type { ImpactFilters } from "./filters";
import styles from "./gov.module.css";

export function SummaryStats({
  stats,
  label,
}: {
  stats: { label: string; value: number }[];
  label: string;
}) {
  return (
    <dl className={styles.summary} aria-label={label}>
      {stats.map((stat) => (
        <div key={stat.label} className={styles.stat}>
          <dt className={styles.statLabel}>{stat.label}</dt>
          <dd className={styles.statValue}>{stat.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export const WITHHELD =
  "Details withheld: you do not have contract access here";

export function FailingRules({ rules }: { rules: ImpactFailingRule[] }) {
  if (rules.length === 0) return <span>None</span>;
  const withheld = rules.some((rule) => rule.pointers_withheld);
  return (
    <div className={styles.tight}>
      <span className={styles.badges}>
        {rules.map((rule, index) => (
          <Badge
            key={`${rule.rule_set_id}-${rule.rule_key}-${String(index)}`}
            intent={rule.blocking ? "danger" : "warning"}
            mono
          >
            {rule.rule_key}
          </Badge>
        ))}
      </span>
      {withheld ? <span className={styles.muted}>{WITHHELD}</span> : null}
    </div>
  );
}

export function ImpactFilterBar({
  idPrefix,
  filters,
  onChange,
  ruleKeys,
  folderIds,
  mode,
}: {
  idPrefix: string;
  filters: ImpactFilters;
  onChange: (filters: ImpactFilters) => void;
  ruleKeys: string[];
  folderIds: string[];
  mode: "dry-run" | "drift";
}) {
  const pathOf = useFolderPathLabel();
  return (
    <div
      className={styles.toolbar}
      role="group"
      aria-label="Filter loaded results"
    >
      {mode === "dry-run" ? (
        <Checkbox
          id={`${idPrefix}-would-block`}
          label="Only would block"
          checked={filters.wouldBlock === true}
          onChange={(event) => {
            onChange({ ...filters, wouldBlock: event.target.checked });
          }}
        />
      ) : (
        <div className={styles.toolbarField}>
          <SelectField
            id={`${idPrefix}-status`}
            label="Status"
            controlSize="small"
            placeholder="Any status"
            value={filters.status ?? ""}
            options={[
              { value: "drifted", label: "Drifted" },
              { value: "at_risk", label: "At risk" },
              { value: "unparseable", label: "Unparseable" },
            ]}
            onChange={(event) => {
              const value = event.target.value;
              onChange({
                ...filters,
                status:
                  value === "" ? undefined : (value as ImpactFilters["status"]),
              });
            }}
          />
        </div>
      )}
      <div className={styles.toolbarField}>
        <SelectField
          id={`${idPrefix}-rule`}
          label="Failing rule"
          controlSize="small"
          placeholder="Any rule"
          value={filters.ruleKey ?? ""}
          options={ruleKeys.map((key) => ({ value: key, label: key }))}
          onChange={(event) => {
            onChange({ ...filters, ruleKey: event.target.value || undefined });
          }}
        />
      </div>
      <div className={styles.toolbarField}>
        <SelectField
          id={`${idPrefix}-folder`}
          label="Folder"
          controlSize="small"
          placeholder="Any folder"
          value={filters.folderId ?? ""}
          options={folderIds.map((id) => ({ value: id, label: pathOf(id) }))}
          onChange={(event) => {
            onChange({ ...filters, folderId: event.target.value || undefined });
          }}
        />
      </div>
    </div>
  );
}
