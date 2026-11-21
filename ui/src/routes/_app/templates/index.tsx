import { createFileRoute } from "@tanstack/react-router";
import { TemplatesPage } from "@/features/templates/TemplatesPage";

type TemplatesSearch = { folder_id?: string };

export const Route = createFileRoute("/_app/templates/")({
  validateSearch: (search: Record<string, unknown>): TemplatesSearch =>
    typeof search.folder_id === "string" && search.folder_id !== ""
      ? { folder_id: search.folder_id }
      : {},
  component: function TemplatesRoute() {
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <TemplatesPage
        folderId={search.folder_id}
        onFolderChange={(folderId) => {
          void navigate({
            search: folderId ? { folder_id: folderId } : {},
            replace: true,
          });
        }}
      />
    );
  },
});
