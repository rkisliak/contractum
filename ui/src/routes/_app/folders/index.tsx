import { createFileRoute } from "@tanstack/react-router";
import { FoldersPage } from "@/features/folders/FoldersPage";

type FoldersSearch = { archived?: boolean; contracts?: boolean };

const isOn = (value: unknown) => value === true || value === "true";

export const Route = createFileRoute("/_app/folders/")({
  // A switch that is off is dropped from the URL, so the default screen has a bare URL.
  validateSearch: (search: Record<string, unknown>): FoldersSearch => ({
    ...(isOn(search.archived) ? { archived: true } : {}),
    ...(isOn(search.contracts) ? { contracts: true } : {}),
  }),
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
          search: (prev) => ({ ...prev, archived: value ? true : undefined }),
          replace: true,
        });
      }}
      showContracts={search.contracts === true}
      onShowContractsChange={(value) => {
        void navigate({
          search: (prev) => ({ ...prev, contracts: value ? true : undefined }),
          replace: true,
        });
      }}
    />
  );
}
