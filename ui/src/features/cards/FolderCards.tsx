// The folder's Subscribers tab: every card in the folder and below it (an Organization lists its
// teams' cards, each row naming its folder), through the same all-cards list as /cards.
import type { Folder } from "@/api/types";
import { CardIndex, type CardIndexFilters } from "./CardIndex";

export type FolderCardsSearch = {
  q?: string;
  archived?: boolean;
  active?: boolean;
};

export function FolderCards({
  folder,
  search,
  onSearchChange,
}: {
  folder: Folder;
  search: FolderCardsSearch;
  onSearchChange: (next: FolderCardsSearch) => void;
}) {
  const filters: CardIndexFilters = {
    folderId: folder.id,
    subtree: true,
    q: search.q ?? "",
    archived: search.archived ?? false,
    active: search.active ?? false,
  };
  return (
    <CardIndex
      key={folder.id}
      idPrefix={`folder-cards-${folder.id}`}
      filters={filters}
      scopeLabel={folder.display_name}
      onFiltersChange={(next) => {
        const merged = { ...filters, ...next };
        onSearchChange({
          ...(merged.q ? { q: merged.q } : {}),
          ...(merged.archived ? { archived: true } : {}),
          ...(merged.active ? { active: true } : {}),
        });
      }}
    />
  );
}
