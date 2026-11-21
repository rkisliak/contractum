// The folder Contracts tab (D-UI-09-05): GET /contracts/contracts?folder_id= with keyset "Load
// more results", a debounced name search and an "Include subfolders" switch (include_subtree).
// Both filters are typed search params, so the view can be bookmarked. Rows open the contract.
import { useNavigate } from "@tanstack/react-router";
import { FileText, Plus } from "lucide-react";
import { useRef, useState } from "react";
import type { ContractSummary, Folder } from "@/api/types";
import { useRouteContext } from "@tanstack/react-router";
import { Badge } from "@/components/Badge";
import { useCapabilities } from "@/features/access/useCapabilities";
import { Button } from "@/components/Button";
import { DataTable, orNotSet, type Column } from "@/components/DataTable";
import { Checkbox, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useCursorList } from "@/lib/useCursorList";
import { problemOf } from "@/lib/unexpected";
import {
  contractLatestColumn,
  contractNameColumn,
  contractOdcsIdColumn,
  contractStatusColumn,
  contractUpdatedColumn,
} from "@/features/contracts/contractColumns";
import styles from "./FolderPages.module.css";

export type FolderContractsSearch = { q?: string; subtree?: boolean };

export function FolderContracts({
  folder,
  search,
  onSearchChange,
}: {
  folder: Folder;
  search: FolderContractsSearch;
  onSearchChange: (next: FolderContractsSearch) => void;
}) {
  const { api } = useRouteContext({ from: "__root__" });
  const navigate = useNavigate();
  const caps = useCapabilities(folder.id);
  const subtree = search.subtree ?? folder.kind !== "team";
  const q = search.q ?? "";
  const [draft, setDraft] = useState(q);
  const timer = useRef<number | undefined>(undefined);

  const list = useCursorList<ContractSummary>({
    queryKey: ["contracts", "list", { folderId: folder.id, q, subtree }],
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/contracts/contracts", {
        params: {
          query: {
            folder_id: folder.id,
            include_subtree: subtree,
            limit: 50,
            ...(q ? { q } : {}),
            ...(cursor === null ? {} : { cursor }),
          },
        },
      });
      if (data === undefined) throw new Error("GET /contracts had no body");
      return data;
    },
  });

  function applyQuery(value: string) {
    window.clearTimeout(timer.current);
    const trimmed = value.trim();
    onSearchChange({ ...search, q: trimmed === "" ? undefined : trimmed });
  }

  const columns: Column<ContractSummary>[] = [
    contractNameColumn,
    contractOdcsIdColumn,
    { id: "domain", header: "Domain", cell: (row) => orNotSet(row.domain) },
    contractStatusColumn,
    {
      id: "tags",
      header: "Tags",
      cell: (row) =>
        row.tags && row.tags.length > 0 ? (
          <span className={styles.tags}>
            {row.tags.map((tag) => (
              <Badge key={tag} intent="neutral">
                {tag}
              </Badge>
            ))}
          </span>
        ) : (
          orNotSet(null)
        ),
    },
    contractLatestColumn,
    contractUpdatedColumn,
  ];

  const filtered = q !== "";

  return (
    <section className={styles.stack} aria-label="Contracts">
      {!folder.archived && caps.can("contract:create") ? (
        <div>
          <Button
            variant="primary"
            size="small"
            icon={Plus}
            onClick={() =>
              void navigate({
                to: "/contracts/new",
                search: { folder: folder.id },
              })
            }
          >
            New contract
          </Button>
        </div>
      ) : null}
      <form
        role="search"
        className={styles.toolbar}
        onSubmit={(event) => {
          event.preventDefault();
          applyQuery(draft);
        }}
      >
        <div className={styles.toolbarField}>
          <TextField
            id="folder-contract-search"
            label="Search contracts"
            type="search"
            autoComplete="off"
            controlSize="small"
            maxLength={200}
            value={draft}
            onChange={(event) => {
              const value = event.target.value;
              setDraft(value);
              window.clearTimeout(timer.current);
              timer.current = window.setTimeout(() => {
                applyQuery(value);
              }, 300);
            }}
          />
        </div>
        {folder.kind !== "team" ? (
          <Checkbox
            id="folder-contract-subtree"
            label="Include subfolders"
            checked={subtree}
            onChange={(event) => {
              onSearchChange({ ...search, subtree: event.target.checked });
            }}
          />
        ) : null}
      </form>
      {list.error ? (
        <ProblemAlert problem={problemOf(list.error)} onRetry={list.refetch} />
      ) : list.isPending ? (
        <Skeleton label="contracts" rows={3} />
      ) : list.rows.length === 0 ? (
        filtered ? (
          <EmptyState
            level={2}
            heading="No contracts match these filters."
            body="Try another search, or clear it to see every contract here."
            action={
              <Button
                variant="secondary"
                size="small"
                onClick={() => {
                  setDraft("");
                  applyQuery("");
                }}
              >
                Clear filters
              </Button>
            }
          />
        ) : (
          <EmptyState
            level={2}
            icon={FileText}
            heading="No contracts yet"
            body={
              folder.kind === "team"
                ? "Contracts created in this team appear here."
                : "Contracts in this folder and the folders below it appear here."
            }
          />
        )
      ) : (
        <DataTable
          caption={`Contracts in ${folder.display_name}`}
          columns={columns}
          rows={list.rows}
          getRowId={(row) => row.id}
          loadMore={{
            hasMore: list.hasMore,
            isLoadingMore: list.isLoadingMore,
            onLoadMore: list.loadMore,
            error: list.pageError,
          }}
        />
      )}
    </section>
  );
}
