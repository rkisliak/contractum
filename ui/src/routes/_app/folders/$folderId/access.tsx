import { createFileRoute } from "@tanstack/react-router";
import { FolderAccess } from "@/features/admin/FolderAccess";
import { useFolder } from "@/features/folders/api";

export const Route = createFileRoute("/_app/folders/$folderId/access")({
  component: function FolderAccessRoute() {
    const { folderId } = Route.useParams();
    const folder = useFolder(folderId);
    return folder.data ? <FolderAccess folder={folder.data} /> : null;
  },
});
