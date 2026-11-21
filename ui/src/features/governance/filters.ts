// Client-side filters over the loaded pages of a dry-run or drift report (D-UI-11-13/14). They
// only narrow what is already loaded; "Load more" fetches the next page from the server.
import type { DriftItem, DryRunItem, ImpactFailingRule } from "@/api/types";
import { compareText } from "@/lib/text";

export type ImpactFilters = {
  /** Dry-run: only contracts whose publish would be blocked. */
  wouldBlock?: boolean;
  /** Drift: only this status. */
  status?: DriftItem["status"];
  ruleKey?: string;
  folderId?: string;
};

type Filterable = {
  folder_id: string;
  failing_rules: ImpactFailingRule[];
};

function matchesCommon(item: Filterable, filters: ImpactFilters): boolean {
  if (filters.folderId && item.folder_id !== filters.folderId) return false;
  if (
    filters.ruleKey &&
    !item.failing_rules.some((rule) => rule.rule_key === filters.ruleKey)
  )
    return false;
  return true;
}

export function filterDryRun(
  items: DryRunItem[],
  filters: ImpactFilters,
): DryRunItem[] {
  return items.filter(
    (item) =>
      (!filters.wouldBlock || item.would_block) && matchesCommon(item, filters),
  );
}

export function filterDrift(
  items: DriftItem[],
  filters: ImpactFilters,
): DriftItem[] {
  return items.filter(
    (item) =>
      (filters.status === undefined || item.status === filters.status) &&
      matchesCommon(item, filters),
  );
}

/** Distinct failing rule keys across the loaded items, sorted, for the rule filter. */
export function ruleKeysOf(items: Filterable[]): string[] {
  const keys = new Set<string>();
  for (const item of items)
    for (const rule of item.failing_rules) keys.add(rule.rule_key);
  return [...keys].sort(compareText);
}

/** Distinct folder ids across the loaded items, for the folder filter. */
export function folderIdsOf(items: Filterable[]): string[] {
  return [...new Set(items.map((item) => item.folder_id))];
}
