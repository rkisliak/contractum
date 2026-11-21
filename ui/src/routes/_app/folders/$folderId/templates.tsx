import { createFileRoute } from "@tanstack/react-router";
import { useFolder } from "@/features/folders/api";
import { FolderTemplates } from "@/features/templates/FolderTemplates";

export const Route = createFileRoute("/_app/folders/$folderId/templates")({
  component: function FolderTemplatesRoute() {
    const { folderId } = Route.useParams();
    const folder = useFolder(folderId);
    return folder.data ? <FolderTemplates folder={folder.data} /> : null;
  },
});
