import { createFileRoute } from "@tanstack/react-router";
import {
  FolderCards,
  type FolderCardsSearch,
} from "@/features/cards/FolderCards";
import { useFolder } from "@/features/folders/api";

// The folder's Subscribers tab: cards in the folder and every folder below it (`q`, `archived`,
// `active` search params).
export const Route = createFileRoute("/_app/folders/$folderId/cards")({
  validateSearch: (search: Record<string, unknown>): FolderCardsSearch => ({
    ...(typeof search.q === "string" && search.q.trim() !== ""
      ? { q: search.q.trim().slice(0, 200) }
      : {}),
    ...(search.archived === true ? { archived: true } : {}),
    ...(search.active === true ? { active: true } : {}),
  }),
  component: function FolderCardsRoute() {
    const { folderId } = Route.useParams();
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    const folder = useFolder(folderId);
    return folder.data ? (
      <FolderCards
        folder={folder.data}
        search={search}
        onSearchChange={(next) => {
          void navigate({ search: next, replace: true });
        }}
      />
    ) : null;
  },
});
