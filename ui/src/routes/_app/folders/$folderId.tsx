import { createFileRoute, Outlet } from "@tanstack/react-router";
import { NotFound } from "@/components/NotFound";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { useFolder } from "@/features/folders/api";
import { FolderHeader } from "@/features/folders/FolderHeader";
import { isNotFound, problemOf } from "@/lib/unexpected";

// The folder layout: header, archived banner and tab navigation around the tab child routes
// (index = Overview, contracts, folders; later phases add templates, rule-sets, cards, access
// and register them in features/folders/tabs.ts). A 404 renders NotFound, never "forbidden".
export const Route = createFileRoute("/_app/folders/$folderId")({
  component: FolderLayout,
});

function FolderLayout() {
  const { folderId } = Route.useParams();
  const folder = useFolder(folderId);
  if (folder.isError) {
    return isNotFound(folder.error) ? (
      <NotFound back={{ label: "folders", to: "/folders" }} />
    ) : (
      <ProblemAlert
        problem={problemOf(folder.error)}
        onRetry={() => void folder.refetch()}
      />
    );
  }
  if (folder.data === undefined) return <Skeleton label="folder" rows={4} />;
  return (
    <>
      <FolderHeader folder={folder.data} />
      <Outlet />
    </>
  );
}
