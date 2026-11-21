import { createFileRoute } from "@tanstack/react-router";
import { useFolder } from "@/features/folders/api";
import {
  FolderContracts,
  type FolderContractsSearch,
} from "@/features/folders/FolderContracts";

export const Route = createFileRoute("/_app/folders/$folderId/contracts")({
  validateSearch: (search: Record<string, unknown>): FolderContractsSearch => ({
    ...(typeof search.q === "string" && search.q.trim() !== ""
      ? { q: search.q.trim().slice(0, 200) }
      : {}),
    ...(typeof search.subtree === "boolean" ? { subtree: search.subtree } : {}),
  }),
  component: function FolderContractsRoute() {
    const { folderId } = Route.useParams();
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    const folder = useFolder(folderId);
    return folder.data ? (
      <FolderContracts
        folder={folder.data}
        search={search}
        onSearchChange={(next) => {
          void navigate({ search: next, replace: true });
        }}
      />
    ) : null;
  },
});
