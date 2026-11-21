// /contracts: every contract the caller can see, across folders (D-UI-09-05 global search).
// GET /contracts/contracts with q, ODCS status and domain filters as typed search params and
// keyset "Load more results". "New contract" opens the creation page. After a delete, the page
// shows the deleted contract with a "Restore contract" action (D-UI-09-07).
import { holdsAny, useHeldAccess } from "@/features/access/useHeldAccess";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { FileText, Plus, Undo2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { Problem } from "@/api/problem";
import type { ContractSummary } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Breakable } from "@/components/Breakable";
import { Button } from "@/components/Button";
import { DataTable, type Column } from "@/components/DataTable";
import { SelectField, TextField } from "@/components/form";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { useFolderList } from "@/features/folders/api";
import { useCursorList } from "@/lib/useCursorList";
import { problemOf } from "@/lib/unexpected";
import { contractKeys, useApi } from "./api";
import {
  contractLatestColumn,
  contractNameColumn,
  contractOdcsIdColumn,
  contractStatusColumn,
  contractUpdatedColumn,
} from "./contractColumns";
import styles from "./Contracts.module.css";

export type ContractsSearch = {
  q?: string;
  status?: string;
  domain?: string;
  deleted?: string;
  deletedName?: string;
  deletedFolder?: string;
};

export const ODCS_STATUSES = [
  "proposed",
  "draft",
  "active",
  "deprecated",
  "retired",
];

function DeletedNotice({
  search,
  onDone,
}: {
  search: ContractsSearch;
  onDone: () => void;
}) {
  const api = useApi();
  const queryClient = useQueryClient();
  const caps = useCapabilities(search.deletedFolder ?? null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [restored, setRestored] = useState(false);
  const id = search.deleted;
  if (!id) return null;
  const name = search.deletedName ?? "The contract";
  if (restored)
    return (
      <Alert intent="success" title={`${name} was restored`} onDismiss={onDone}>
        <Link
          to="/contracts/$contractId"
          params={{ contractId: id }}
          className={styles.rowLink}
        >
          Open {name}
        </Link>
      </Alert>
    );
  return (
    <Alert
      intent="success"
      title={`${name} was deleted`}
      onDismiss={onDone}
      action={
        caps.can("contract:restore") ? (
          <Button
            variant="secondary"
            size="small"
            icon={Undo2}
            busy={busy}
            busyLabel="Restoring…"
            onClick={() => {
              setBusy(true);
              setProblem(null);
              api
                .POST("/api/v1/contracts/contracts/{contract_id}/restore", {
                  params: { path: { contract_id: id } },
                })
                .then(
                  async () => {
                    setRestored(true);
                    await queryClient.invalidateQueries({
                      queryKey: contractKeys.lists,
                    });
                  },
                  (error: unknown) => {
                    setProblem(problemOf(error));
                  },
                )
                .finally(() => {
                  setBusy(false);
                });
            }}
          >
            Restore contract
          </Button>
        ) : undefined
      }
    >
      <p>
        It no longer appears in lists or fetches. An Organization admin can
        restore it.
      </p>
      {problem ? <ProblemAlert problem={problem} context="action" /> : null}
    </Alert>
  );
}

export function ContractsPage({
  search,
  onSearchChange,
}: {
  search: ContractsSearch;
  onSearchChange: (next: ContractsSearch) => void;
}) {
  const api = useApi();
  const navigate = useNavigate();
  const folders = useFolderList(true).data?.folders ?? [];
  const folderById = new Map(folders.map((folder) => [folder.id, folder]));
  const q = search.q ?? "";
  const [draft, setDraft] = useState(q);
  const [domainDraft, setDomainDraft] = useState(search.domain ?? "");
  const timer = useRef<number | undefined>(undefined);
  // A pending debounced search must not fire after the page is left (for example a result
  // clicked within the debounce window), or it would navigate back to /contracts.
  useEffect(
    () => () => {
      window.clearTimeout(timer.current);
    },
    [],
  );

  const list = useCursorList<ContractSummary>({
    queryKey: [
      ...contractKeys.lists,
      "list",
      { q, status: search.status, domain: search.domain },
    ],
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/contracts/contracts", {
        params: {
          query: {
            limit: 50,
            ...(q ? { q } : {}),
            ...(search.status ? { status: search.status } : {}),
            ...(search.domain ? { domain: search.domain } : {}),
            ...(cursor === null ? {} : { cursor }),
          },
        },
      });
      if (data === undefined) throw new Error("GET /contracts had no body");
      return data;
    },
  });

  const filters = { q: search.q, status: search.status, domain: search.domain };
  // The debounced search applies 300 ms later: it must merge with the filters current then, not
  // with the ones of the render that scheduled it (a domain removed on blur in the same moment
  // would otherwise come back).
  const currentFilters = useRef(filters);
  useEffect(() => {
    currentFilters.current = filters;
  });

  function apply(next: Partial<ContractsSearch>) {
    window.clearTimeout(timer.current);
    const merged = { ...currentFilters.current, ...next };
    onSearchChange({
      ...(merged.q ? { q: merged.q } : {}),
      ...(merged.status ? { status: merged.status } : {}),
      ...(merged.domain ? { domain: merged.domain } : {}),
    });
  }

  const columns: Column<ContractSummary>[] = [
    contractNameColumn,
    contractOdcsIdColumn,
    {
      id: "folder",
      header: "Folder",
      sortValue: (row) => folderById.get(row.folder_id)?.path ?? row.folder_id,
      cell: (row) => {
        const folder = folderById.get(row.folder_id);
        return (
          <Link
            to="/folders/$folderId/contracts"
            params={{ folderId: row.folder_id }}
            className={styles.rowLink}
          >
            {folder ? <Breakable text={folder.path} /> : "Folder"}
          </Link>
        );
      },
    },
    contractLatestColumn,
    contractStatusColumn,
    contractUpdatedColumn,
  ];

  const filtered = Boolean(q || search.status || search.domain);
  // "New contract" only when contract:create is held somewhere (the page picks the folder).
  const held = useHeldAccess();
  const canCreate =
    held.data !== undefined && holdsAny(held.data, "contract:create");

  return (
    <>
      <PageHeader
        title="Contracts"
        breadcrumbs={[]}
        description="Every contract you can see, across all folders."
        actions={
          canCreate ? (
            <Button
              variant="primary"
              size="small"
              icon={Plus}
              onClick={() => void navigate({ to: "/contracts/new" })}
            >
              New contract
            </Button>
          ) : undefined
        }
      />
      <div className={styles.stack}>
        <DeletedNotice
          search={search}
          onDone={() => {
            apply({});
          }}
        />
        <form
          role="search"
          className={styles.toolbar}
          onSubmit={(event) => {
            event.preventDefault();
            apply({
              q: draft.trim() || undefined,
              domain: domainDraft.trim() || undefined,
            });
          }}
        >
          <div className={styles.toolbarField}>
            <TextField
              id="contract-search"
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
                  apply({ q: value.trim() || undefined });
                }, 300);
              }}
            />
          </div>
          <div className={styles.toolbarField}>
            <SelectField
              id="contract-status"
              label="ODCS status"
              controlSize="small"
              placeholder="Any status"
              options={ODCS_STATUSES.map((status) => ({
                value: status,
                label: status,
              }))}
              value={search.status ?? ""}
              onChange={(event) => {
                apply({ status: event.target.value || undefined });
              }}
            />
          </div>
          <div className={styles.toolbarField}>
            <TextField
              id="contract-domain"
              label="Domain"
              controlSize="small"
              autoComplete="off"
              value={domainDraft}
              onChange={(event) => {
                setDomainDraft(event.target.value);
              }}
              onBlur={() => {
                apply({ domain: domainDraft.trim() || undefined });
              }}
            />
          </div>
        </form>
        {list.error ? (
          <ProblemAlert
            problem={problemOf(list.error)}
            onRetry={list.refetch}
          />
        ) : list.isPending ? (
          <Skeleton label="contracts" rows={4} />
        ) : list.rows.length === 0 ? (
          filtered ? (
            <EmptyState
              heading="No contracts match these filters."
              body="Try another search, or clear the filters to see every contract."
              action={
                <Button
                  variant="secondary"
                  size="small"
                  onClick={() => {
                    setDraft("");
                    setDomainDraft("");
                    onSearchChange({});
                  }}
                >
                  Clear filters
                </Button>
              }
            />
          ) : (
            <EmptyState
              icon={FileText}
              heading="No contracts yet"
              body={
                canCreate
                  ? "Create the first one with New contract above."
                  : "Contracts published in the folders you can see appear here."
              }
            />
          )
        ) : (
          <DataTable
            caption="Contracts"
            captionHidden
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
      </div>
    </>
  );
}
