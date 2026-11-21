// Every subscriber card the caller can read, across folders: GET /contracts/cards with a folder
// scope (null = all folders, the Root view) and subtree, search, "Show archived" and "Only with
// active subscriptions", keyset "Load more results" and the "N cards in M folders" summary (the
// API counts only cards the caller can open). Shared by the Subscriptions page (/cards) and the
// folder's Subscribers tab, where an Organization lists its teams' cards with each row's folder.
import { Link } from "@tanstack/react-router";
import { Bell, Plus } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import type {
  Folder,
  SubscriberCardIndex,
  SubscriberCardIndexItem,
} from "@/api/types";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { DataTable, type Column } from "@/components/DataTable";
import { Checkbox, TextField, type SelectOption } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { holdsAny, useHeldAccess } from "@/features/access/useHeldAccess";
import { useFolderList } from "@/features/folders/api";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { useCursorList } from "@/lib/useCursorList";
import { problemOf } from "@/lib/unexpected";
import { cardKeys, useApi } from "./api";
import { CardFormDialog } from "./CardFormDialog";
import styles from "./Cards.module.css";

export type CardIndexFilters = {
  /** The scope folder; null lists every folder (Root). */
  folderId: string | null;
  subtree: boolean;
  q: string;
  archived: boolean;
  active: boolean;
};

/** "Acme / Payments / Fraud" from the parent chain of the readable folders. */
export function folderLabel(folder: Folder, byId: Map<string, Folder>): string {
  const names: string[] = [];
  let current: Folder | undefined = folder;
  let guard = 0;
  while (current && current.kind !== "root" && guard < 10) {
    names.unshift(current.display_name);
    current = current.parent_id ? byId.get(current.parent_id) : undefined;
    guard += 1;
  }
  return names.join(" / ") || folder.display_name;
}

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
}

export function CardIndex({
  filters,
  onFiltersChange,
  scopeLabel,
  scopeControls,
  idPrefix,
}: {
  filters: CardIndexFilters;
  onFiltersChange: (next: Partial<CardIndexFilters>) => void;
  /** "all folders" or the scope folder's name, for the caption and empty state. */
  scopeLabel: string;
  /** Extra toolbar controls before the search box (the scope picker). */
  scopeControls?: ReactNode;
  idPrefix: string;
}) {
  const api = useApi();
  const folderList = useFolderList(true);
  const all = folderList.data?.folders ?? [];
  const byId = new Map(all.map((folder) => [folder.id, folder]));
  const scope = filters.folderId ? byId.get(filters.folderId) : undefined;
  const scopeCaps = useCapabilities(filters.folderId);
  const held = useHeldAccess();
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState(filters.q);
  const timer = useRef<number | undefined>(undefined);

  const list = useCursorList<SubscriberCardIndexItem, SubscriberCardIndex>({
    queryKey: cardKeys.index(filters),
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/contracts/cards", {
        params: {
          query: {
            limit: 50,
            include_archived: filters.archived,
            ...(filters.folderId
              ? { folder_id: filters.folderId, subtree: filters.subtree }
              : {}),
            ...(filters.q ? { q: filters.q } : {}),
            ...(filters.active ? { has_active_subscriptions: true } : {}),
            ...(cursor === null ? {} : { cursor }),
          },
        },
      });
      if (data === undefined) throw new Error("GET /cards had no body");
      return data;
    },
  });

  // Where a new card can go: the scope itself when it is a Team (or a folder listed without its
  // subfolders), else a folder chosen in the dialog among every non-archived folder in scope, Root
  // first. The API re-checks card:create.
  const inScope = (folder: Folder) =>
    !filters.folderId ||
    folder.id === filters.folderId ||
    (filters.subtree &&
      scope !== undefined &&
      folder.path.startsWith(`${scope.path}/`));
  const choices: SelectOption[] = all
    .filter((folder) => !folder.archived && inScope(folder))
    .sort(
      (a, b) =>
        Number(b.kind === "root") - Number(a.kind === "root") ||
        folderLabel(a, byId).localeCompare(folderLabel(b, byId)),
    )
    .map((folder) => ({ value: folder.id, label: folderLabel(folder, byId) }));
  // A Team has no subfolders, so its scope is always the card's folder.
  const fixedFolder =
    scope && (scope.kind === "team" || !filters.subtree) ? scope : undefined;
  // Without a fixed folder, offer it when card:create is held somewhere (the dialog's folder
  // choice and the API decide where).
  const canCreate = fixedFolder
    ? scopeCaps.can("card:create") && !fixedFolder.archived
    : choices.length > 0 &&
      held.data !== undefined &&
      holdsAny(held.data, "card:create");

  const createButton = canCreate ? (
    <Button
      variant="primary"
      size="small"
      icon={Plus}
      onClick={() => {
        setCreating(true);
      }}
    >
      Create card
    </Button>
  ) : null;

  function folderCell(row: SubscriberCardIndexItem) {
    const folder = byId.get(row.folder_id);
    if (!folder)
      return <span className={styles.muted}>Folder not visible to you</span>;
    return (
      <Link
        to="/folders/$folderId"
        params={{ folderId: row.folder_id }}
        className={styles.rowLink}
      >
        {folderLabel(folder, byId)}
      </Link>
    );
  }

  const columns: Column<SubscriberCardIndexItem>[] = [
    {
      id: "name",
      header: "Card",
      rowHeader: true,
      sortValue: (row) => row.name.toLowerCase(),
      cell: (row) => (
        <span className={styles.inline}>
          <Link
            to="/cards/$cardId"
            params={{ cardId: row.id }}
            className={styles.rowLink}
          >
            {row.name}
          </Link>
          {row.archived_at ? <Badge intent="neutral">Archived</Badge> : null}
        </span>
      ),
    },
    {
      id: "folder",
      header: "Folder",
      sortValue: (row) => {
        const folder = byId.get(row.folder_id);
        return folder ? folderLabel(folder, byId) : "";
      },
      cell: folderCell,
    },
    {
      id: "active",
      header: "Active subscriptions",
      align: "end",
      sortValue: (row) => row.active_subscription_count,
      cell: (row) => String(row.active_subscription_count),
    },
    {
      id: "contracts",
      header: "Subscribed contracts",
      cell: (row) => {
        if (row.subscriptions.length === 0)
          return <span className={styles.muted}>None</span>;
        const more = row.active_subscription_count - row.subscriptions.length;
        return (
          <span className={styles.inline}>
            {row.subscriptions.map((pin) => (
              <Badge key={pin.version_id} intent="neutral">
                {`${pin.contract_name ?? "Restricted contract"} ${pin.semver}`}
              </Badge>
            ))}
            {more > 0 ? (
              <span className={styles.muted}>{`+${String(more)} more`}</span>
            ) : null}
          </span>
        );
      },
    },
    {
      id: "updated",
      header: "Updated",
      sortValue: (row) => row.updated_at,
      cell: (row) => formatDateTime(row.updated_at),
    },
  ];

  const filtered = Boolean(filters.q || filters.active);
  const emptyScope =
    !list.error && !list.isPending && list.rows.length === 0 && !filtered;
  const total = list.firstPage?.total_cards ?? 0;
  const folderCount = list.firstPage?.total_folders ?? 0;

  function applyQuery(value: string) {
    window.clearTimeout(timer.current);
    onFiltersChange({ q: value.trim() });
  }

  return (
    <section className={styles.stack} aria-label="Subscriber cards">
      <form
        role="search"
        className={styles.toolbarStart}
        onSubmit={(event) => {
          event.preventDefault();
          applyQuery(draft);
        }}
      >
        {scopeControls}
        <div className={styles.picker}>
          <TextField
            id={`${idPrefix}-search`}
            label="Search cards"
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
      </form>
      <div className={styles.toolbar}>
        <div className={styles.toolbarStart}>
          <Checkbox
            id={`${idPrefix}-archived`}
            label="Show archived"
            checked={filters.archived}
            onChange={(event) => {
              onFiltersChange({ archived: event.target.checked });
            }}
          />
          <Checkbox
            id={`${idPrefix}-active`}
            label="Only with active subscriptions"
            checked={filters.active}
            onChange={(event) => {
              onFiltersChange({ active: event.target.checked });
            }}
          />
        </div>
        {/* The empty state carries the only "Create card" when nothing is in scope. */}
        {emptyScope ? null : createButton}
      </div>
      {list.error ? (
        <ProblemAlert problem={problemOf(list.error)} onRetry={list.refetch} />
      ) : list.isPending ? (
        <Skeleton label="subscriber cards" rows={3} />
      ) : list.rows.length === 0 ? (
        filtered ? (
          <EmptyState
            level={2}
            heading="No cards match these filters"
            body="Try another search, or clear the filters to see every card in scope."
            action={
              <Button
                variant="secondary"
                size="small"
                onClick={() => {
                  setDraft("");
                  onFiltersChange({ q: "", active: false });
                }}
              >
                Clear filters
              </Button>
            }
          />
        ) : (
          <EmptyState
            level={2}
            icon={Bell}
            heading="No subscriber cards yet"
            body={`There are no cards you can see in ${scopeLabel}. A card represents a consuming team: create one in any folder and subscribe it to contract versions to get notices when they change.`}
            action={createButton}
          />
        )
      ) : (
        <>
          <p className={styles.muted} aria-live="polite">
            {`${plural(total, "card", "cards")} in ${plural(folderCount, "folder", "folders")}`}
          </p>
          <DataTable
            caption={`Subscriber cards in ${scopeLabel}`}
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
        </>
      )}
      <CardFormDialog
        // Remount on open so the preselected folder reflects the folders loaded by then.
        key={creating ? "open" : "closed"}
        open={creating}
        onOpenChange={setCreating}
        {...(fixedFolder
          ? { folderId: fixedFolder.id, folderName: fixedFolder.display_name }
          : {
              folderChoices: choices,
              ...(filters.folderId &&
              choices.some((choice) => choice.value === filters.folderId)
                ? { initialFolderId: filters.folderId }
                : {}),
            })}
      />
    </section>
  );
}
