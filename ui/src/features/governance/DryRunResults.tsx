// The results of one dry-run (D-UI-11-13): POST /contracts/governance/rule-set-dry-runs pages
// through the published contracts in scope with the returned cursor ("Load more results").
// Summary cards add the page totals; the table links every contract to /contracts/$id, where
// the API decides what the caller may see. No contract document is ever fetched here.
import { useInfiniteQuery } from "@tanstack/react-query";
import { Link, useRouteContext } from "@tanstack/react-router";
import { useState } from "react";
import type { DryRunItem, DryRunResult } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Breakable } from "@/components/Breakable";
import { Identifier } from "@/components/Identifier";
import { DataTable, type Column } from "@/components/DataTable";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useFolderPathLabel } from "@/features/folders/FolderSelect";
import { problemOf } from "@/lib/unexpected";
import {
  filterDryRun,
  folderIdsOf,
  ruleKeysOf,
  type ImpactFilters,
} from "./filters";
import { FailingRules, ImpactFilterBar, SummaryStats } from "./ImpactParts";
import styles from "./gov.module.css";

export type DryRunRequest = {
  ruleSetId: string;
  version: number | "draft";
  folderId?: string;
  /** Changes on every "Run dry-run" so the same request can be run again. */
  run: number;
};

export function DryRunResults({ request }: { request: DryRunRequest }) {
  const { api } = useRouteContext({ from: "__root__" });
  const pathOf = useFolderPathLabel();
  const [filters, setFilters] = useState<ImpactFilters>({});
  const query = useInfiniteQuery({
    queryKey: ["governance", "dry-run", request],
    initialPageParam: null as string | null,
    getNextPageParam: (last: DryRunResult) => last.next_cursor ?? undefined,
    queryFn: async ({ pageParam }): Promise<DryRunResult> => {
      const { data } = await api.POST(
        "/api/v1/contracts/governance/rule-set-dry-runs",
        {
          body: {
            rule_set_id: request.ruleSetId,
            version: request.version,
            ...(request.folderId ? { folder_id: request.folderId } : {}),
            ...(pageParam === null ? {} : { cursor: pageParam }),
          },
        },
      );
      if (data === undefined) throw new Error("POST dry-run had no body");
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
    return <Skeleton label="dry-run results" rows={4} />;

  const pages = query.data.pages;
  const items = pages.flatMap((page) => page.items);
  const sum = (pick: (page: DryRunResult) => number) =>
    pages.reduce((total, page) => total + pick(page), 0);
  const total = pages[0]?.total_in_scope ?? 0;
  const rows = filterDryRun(items, filters);

  const columns: Column<DryRunItem>[] = [
    {
      id: "contract",
      header: "Contract and outcome",
      rowHeader: true,
      sortValue: (row) => row.name ?? row.odcs_id,
      // The outcome badge sits under the name, so the table fits a desktop width.
      cell: (row) => (
        <div className={styles.cellStack}>
          <Link
            to="/contracts/$contractId"
            params={{ contractId: row.contract_id }}
            className={styles.rowLink}
          >
            {row.name ?? row.odcs_id}
          </Link>
          {row.status === "unparseable" ? (
            <Badge intent="warning">Unparseable</Badge>
          ) : row.would_block ? (
            <Badge intent="danger">Would block</Badge>
          ) : row.failing_rules.length > 0 ? (
            <Badge intent="warning">Warnings</Badge>
          ) : (
            <Badge intent="success">Passes</Badge>
          )}
        </div>
      ),
    },
    {
      id: "odcs",
      header: "ODCS id",
      cell: (row) => <Identifier value={row.odcs_id} max="12rem" />,
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
        `${String(row.counts.fail)} / ${String(row.counts.error)} / ${String(row.counts.warn)}`.replace(
          / /g,
          "\u00a0",
        ),
    },
    {
      id: "rules",
      header: "Failing rules",
      cell: (row) => <FailingRules rules={row.failing_rules} />,
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
        label="Dry-run summary"
        stats={[
          {
            label: "Would fail",
            value: sum((page) => page.summary.would_fail),
          },
          {
            label: "Would error",
            value: sum((page) => page.summary.would_error),
          },
          { label: "Pass", value: sum((page) => page.summary.pass) },
          { label: "Hidden", value: sum((page) => page.summary.hidden) },
        ]}
      />
      {items.length === 0 ? (
        <EmptyState
          level={3}
          heading="No affected contracts listed"
          body="No published contract in scope is listed for this version. Contracts you may not see are counted as hidden."
        />
      ) : (
        <>
          <ImpactFilterBar
            idPrefix="dry-run"
            mode="dry-run"
            filters={filters}
            onChange={setFilters}
            ruleKeys={ruleKeysOf(items)}
            folderIds={folderIdsOf(items)}
          />
          <DataTable
            caption="Dry-run results"
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
