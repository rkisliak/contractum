import { createFileRoute } from "@tanstack/react-router";
import { CardsPage, type CardsSearch } from "@/features/cards/CardsPage";

// The Subscriptions page: every visible subscriber card; typed scope, search and filter params.
export const Route = createFileRoute("/_app/cards/")({
  validateSearch: (search: Record<string, unknown>): CardsSearch => ({
    ...(typeof search.folder === "string" && search.folder !== ""
      ? { folder: search.folder.slice(0, 64) }
      : {}),
    ...(search.subtree === false ? { subtree: false as const } : {}),
    ...(typeof search.q === "string" && search.q.trim() !== ""
      ? { q: search.q.trim().slice(0, 200) }
      : {}),
    ...(search.archived === true ? { archived: true } : {}),
    ...(search.active === true ? { active: true } : {}),
  }),
  component: function CardsRoute() {
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <CardsPage
        search={search}
        onSearchChange={(next) => {
          void navigate({ search: next, replace: true });
        }}
      />
    );
  },
});
