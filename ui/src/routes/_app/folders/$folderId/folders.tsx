import { createFileRoute } from "@tanstack/react-router";
import { useFolder } from "@/features/folders/api";
import { FolderChildren } from "@/features/folders/FolderChildren";

export const Route = createFileRoute("/_app/folders/$folderId/folders")({
  component: function FolderChildrenRoute() {
    const { folderId } = Route.useParams();
    const folder = useFolder(folderId);
    return folder.data ? <FolderChildren folder={folder.data} /> : null;
  },
});
