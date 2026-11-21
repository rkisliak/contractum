import { createFileRoute } from "@tanstack/react-router";
import { DriftPage } from "@/features/governance/DriftPage";

type DriftSearch = { folder_id?: string };

export const Route = createFileRoute("/_app/governance/drift")({
  validateSearch: (search: Record<string, unknown>): DriftSearch =>
    typeof search.folder_id === "string" && search.folder_id !== ""
      ? { folder_id: search.folder_id }
      : {},
  component: function DriftRoute() {
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <DriftPage
        folderId={search.folder_id}
        onFolderChange={(folderId) => {
          void navigate({ search: { folder_id: folderId }, replace: true });
        }}
      />
    );
  },
});
