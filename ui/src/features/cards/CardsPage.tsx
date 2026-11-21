// The Subscriptions page (/cards): every subscriber card the caller can see. The scope picker
// starts at "All folders (Root)"; picking a folder lists it and, with "Include subfolders" (on by
// default), everything below it. Scope, search and filters are typed search params, so a view
// can be bookmarked and shared.
import { PageHeader } from "@/components/PageHeader";
import { Checkbox, SelectField } from "@/components/form";
import { kindLabel } from "@/features/access/roles";
import { useFolderList } from "@/features/folders/api";
import { CardIndex, folderLabel, type CardIndexFilters } from "./CardIndex";
import styles from "./Cards.module.css";

export type CardsSearch = {
  folder?: string;
  /** Only stored when off: subfolders are included by default. */
  subtree?: false;
  q?: string;
  archived?: boolean;
  active?: boolean;
};

export function toCardsSearch(filters: CardIndexFilters): CardsSearch {
  return {
    ...(filters.folderId ? { folder: filters.folderId } : {}),
    ...(filters.folderId && !filters.subtree
      ? { subtree: false as const }
      : {}),
    ...(filters.q ? { q: filters.q } : {}),
    ...(filters.archived ? { archived: true } : {}),
    ...(filters.active ? { active: true } : {}),
  };
}

export function CardsPage({
  search,
  onSearchChange,
}: {
  search: CardsSearch;
  onSearchChange: (next: CardsSearch) => void;
}) {
  const list = useFolderList(true);
  const all = list.data?.folders ?? [];
  const byId = new Map(all.map((folder) => [folder.id, folder]));
  const folders = all.filter((folder) => folder.kind !== "root");
  const scope = search.folder ? byId.get(search.folder) : undefined;
  const filters: CardIndexFilters = {
    folderId: search.folder ?? null,
    subtree: search.subtree !== false,
    q: search.q ?? "",
    archived: search.archived ?? false,
    active: search.active ?? false,
  };
  const options = folders
    .map((folder) => ({
      value: folder.id,
      label: `${folderLabel(folder, byId)} (${kindLabel(folder.kind)})`,
    }))
    .sort((a, b) => a.label.localeCompare(b.label));
  const change = (next: Partial<CardIndexFilters>) => {
    onSearchChange(toCardsSearch({ ...filters, ...next }));
  };
  const scopeLabel = scope
    ? folderLabel(scope, byId)
    : search.folder
      ? "this folder"
      : "all folders";

  return (
    <div className={styles.stack}>
      <PageHeader
        title="Subscriptions"
        breadcrumbs={[]}
        description="Subscriber cards represent consuming teams. Each card pins contract versions and receives notices when they change."
      />
      <CardIndex
        idPrefix="cards"
        filters={filters}
        onFiltersChange={change}
        scopeLabel={scopeLabel}
        scopeControls={
          <>
            <div className={styles.picker}>
              <SelectField
                id="cards-scope"
                label="Scope"
                controlSize="small"
                placeholder="All folders (Root)"
                options={options}
                value={search.folder ?? ""}
                onChange={(event) => {
                  change({ folderId: event.target.value || null });
                }}
              />
            </div>
            <div className={styles.checkboxSlot}>
              <Checkbox
                id="cards-subtree"
                label="Include subfolders"
                checked={filters.subtree}
                disabled={!search.folder}
                onChange={(event) => {
                  change({ subtree: event.target.checked });
                }}
              />
            </div>
          </>
        }
      />
    </div>
  );
}
