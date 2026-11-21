// /governance/drift?folder_id= (D-UI-11-14): published contracts whose Latest no longer passes
// the mandatory rule-set versions that apply now, or will not once a warning version enforces.
// Compliant contracts are counted, not listed. Paging follows `next_cursor`.
import { useInfiniteQuery } from "@tanstack/react-query";
import { Link, useRouteContext } from "@tanstack/react-router";
import { useState } from "react";
import type { DriftItem, DriftResult } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Breakable } from "@/components/Breakable";
import { DataTable, type Column } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, NotAvailable, Skeleton } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { useFolderList } from "@/features/folders/api";
import {
  FolderSelect,
  useFolderPathLabel,
} from "@/features/folders/FolderSelect";
import { problemOf } from "@/lib/unexpected";
import {
  filterDrift,
  folderIdsOf,
  ruleKeysOf,
  type ImpactFilters,
} from "./filters";
import { FailingRules, ImpactFilterBar, SummaryStats } from "./ImpactParts";
import styles from "./gov.module.css";

const statusBadge: Record<
  DriftItem["status"],
  { label: string; intent: "danger" | "warning" | "neutral" }
> = {
  drifted: { label: "Drifted", intent: "danger" },
  at_risk: { label: "At risk", intent: "warning" },
  unparseable: { label: "Unparseable", intent: "neutral" },
};

export function DriftPage({
  folderId,
  onFolderChange,
}: {
  folderId: string | undefined;
  onFolderChange: (folderId: string) => void;
}) {
  const list = useFolderList();
  const folders = list.data?.folders ?? [];
  const selected =
    folders.find((folder) => folder.id === folderId) ??
    folders.find((folder) => folder.kind === "root") ??
    folders[0];
  return (
    <>
      <PageHeader
        title="Drift"
        description="Published contracts that no longer pass the mandatory rule sets that apply to them, or will not once a warning version starts blocking."
        breadcrumbs={[{ label: "Governance" }]}
      />
      <div className={styles.stack}>
        <div className={styles.toolbar}>
          <div className={styles.toolbarField}>
            <FolderSelect
              id="drift-scope-folder"
              label="Folder"
              value={selected?.id ?? ""}
              onChange={onFolderChange}
            />
          </div>
        </div>
        {selected === undefined ? (
          list.isPending ? (
            <Skeleton label="folders" rows={2} />
          ) : (
            <p className={styles.muted}>You cannot see any folder yet.</p>
          )
        ) : (
          <GatedDriftReport key={selected.id} folderId={selected.id} />
        )}
      </div>
    </>
  );
}

/** The report needs governance:drift-read at the folder; without it a calm note replaces it. */
function GatedDriftReport({ folderId }: { folderId: string }) {
  const caps = useCapabilities(folderId);
  if (caps.isPending) return <Skeleton label="drift report" rows={4} />;
  if (!caps.can("governance:drift-read"))
    return <NotAvailable what="The drift report for this folder" />;
  return <DriftReport folderId={folderId} />;
}

function DriftReport({ folderId }: { folderId: string }) {
  const { api } = useRouteContext({ from: "__root__" });
  const pathOf = useFolderPathLabel();
  const [filters, setFilters] = useState<ImpactFilters>({});
  const query = useInfiniteQuery({
    queryKey: ["governance", "drift", folderId],
    initialPageParam: null as string | null,
    getNextPageParam: (last: DriftResult) => last.next_cursor ?? undefined,
    queryFn: async ({ pageParam }): Promise<DriftResult> => {
      const { data } = await api.GET("/api/v1/contracts/governance/drift", {
        params: {
          query: {
            folder_id: folderId,
            ...(pageParam === null ? {} : { cursor: pageParam }),
          },
        },
      });
      if (data === undefined) throw new Error("GET drift had no body");
      return data;
    },
  });

  if (query.isError && query.data === undefined)
    return (
      <ProblemAlert
        problem={problemOf(query.error)}
        onRetry={() => void query.refetch()}
      />
    );
  if (query.data === undefined)
    return <Skeleton label="drift report" rows={4} />;
  const pages = query.data.pages;
  const items = pages.flatMap((page) => page.items);
  const sum = (pick: (page: DriftResult) => number) =>
    pages.reduce((total, page) => total + pick(page), 0);
  const total = pages[0]?.total_in_scope ?? 0;
  const rows = filterDrift(items, filters);

  const columns: Column<DriftItem>[] = [
    {
      id: "contract",
      header: "Contract and status",
      rowHeader: true,
      sortValue: (row) => row.name ?? row.odcs_id,
      // The status badge sits under the name, so the wide table keeps its prose column readable.
      cell: (row) => (
        <div className={styles.cellStack}>
          <Link
            to="/contracts/$contractId"
            params={{ contractId: row.contract_id }}
            className={styles.rowLink}
          >
            {row.name ?? row.odcs_id}
          </Link>
          <Badge intent={statusBadge[row.status].intent}>
            {statusBadge[row.status].label}
          </Badge>
        </div>
      ),
    },
    {
      id: "folder",
      header: "Folder",
      cell: (row) => (
        <span className={styles.ident}>
          <Breakable text={pathOf(row.folder_id)} />
        </span>
      ),
    },
    {
      id: "semver",
      header: "Latest",
      cell: (row) => row.latest_semver ?? "Not published",
    },
    {
      id: "counts",
      header: "Fail / error / warn",
      align: "end",
      cell: (row) =>
        // No-break spaces: the three counts always read as one value.
        `${String(row.counts.fail)}\u00a0/ ${String(row.counts.error)}\u00a0/ ${String(row.counts.warn)}`.replace(
          / /g,
          "\u00a0",
        ),
    },
    {
      id: "rules",
      header: "Failing rules",
      cell: (row) => <FailingRules rules={row.failing_rules} />,
    },
    {
      id: "pins",
      header: "Pinned vs current",
      cell: (row) => (
        <div className={styles.pins}>
          <span>
            Pinned:{" "}
            {row.pinned_rule_sets.length === 0
              ? "none"
              : row.pinned_rule_sets
                  .map(
                    (pin) =>
                      `${pin.name ?? "rule set"} v${String(pin.version ?? "?")}`,
                  )
                  .join(", ")}
          </span>
          <span>
            Current:{" "}
            {row.current_versions.length === 0
              ? "none"
              : row.current_versions
                  .map((v) => `${v.name} v${String(v.version_no)} (${v.mode})`)
                  .join(", ")}
          </span>
        </div>
      ),
    },
  ];

  return (
    <div className={styles.stack}>
      <p className={styles.muted}>
        {total === 1
          ? "1 contract in scope"
          : `${String(total)} contracts in scope`}
      </p>
      <SummaryStats
        label="Drift summary"
        stats={[
          { label: "Drifted", value: sum((page) => page.summary.drifted) },
          { label: "At risk", value: sum((page) => page.summary.at_risk) },
          { label: "Compliant", value: sum((page) => page.summary.compliant) },
          {
            label: "Unparseable",
            value: sum((page) => page.summary.unparseable),
          },
          { label: "Hidden", value: sum((page) => page.summary.hidden) },
        ]}
      />
      {items.length === 0 ? (
        <EmptyState
          level={3}
          heading="No drift"
          body="Every published contract you can see here passes the mandatory rule sets that apply to it."
        />
      ) : (
        <>
          <ImpactFilterBar
            idPrefix="drift"
            mode="drift"
            filters={filters}
            onChange={setFilters}
            ruleKeys={ruleKeysOf(items)}
            folderIds={folderIdsOf(items)}
          />
          <DataTable
            caption="Drifted contracts"
            captionHidden
            compact
            columns={columns}
            rows={rows}
            getRowId={(row) => row.contract_id}
            loadMore={{
              hasMore: query.hasNextPage,
              isLoadingMore: query.isFetchingNextPage,
              onLoadMore: () => void query.fetchNextPage(),
              error: query.isFetchNextPageError ? query.error : null,
            }}
          />
        </>
      )}
    </div>
  );
}
