import { createFileRoute } from "@tanstack/react-router";
import { useFolder } from "@/features/folders/api";
import { FolderOverview } from "@/features/folders/FolderOverview";

export const Route = createFileRoute("/_app/folders/$folderId/")({
  component: function FolderOverviewRoute() {
    const { folderId } = Route.useParams();
    const folder = useFolder(folderId);
    return folder.data ? <FolderOverview folder={folder.data} /> : null;
  },
});
