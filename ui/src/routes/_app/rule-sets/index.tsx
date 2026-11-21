import { createFileRoute } from "@tanstack/react-router";
import { RuleSetsPage } from "@/features/rule-sets/RuleSetsPage";

type RuleSetsSearch = { folder_id?: string };

export const Route = createFileRoute("/_app/rule-sets/")({
  validateSearch: (search: Record<string, unknown>): RuleSetsSearch =>
    typeof search.folder_id === "string" && search.folder_id !== ""
      ? { folder_id: search.folder_id }
      : {},
  component: function RuleSetsRoute() {
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <RuleSetsPage
        folderId={search.folder_id}
        onFolderChange={(folderId) => {
          void navigate({ search: { folder_id: folderId }, replace: true });
        }}
      />
    );
  },
});
