import { createFileRoute } from "@tanstack/react-router";
import { FoldersPage } from "@/features/folders/FoldersPage";

type FoldersSearch = { archived?: boolean };

export const Route = createFileRoute("/_app/folders/")({
  validateSearch: (search: Record<string, unknown>): FoldersSearch =>
    search.archived === true || search.archived === "true"
      ? { archived: true }
      : {},
  component: FoldersRoute,
});

function FoldersRoute() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  return (
    <FoldersPage
      showArchived={search.archived === true}
      onShowArchivedChange={(value) => {
        void navigate({
          search: value ? { archived: true } : {},
          replace: true,
        });
      }}
    />
  );
}
