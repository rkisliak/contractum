import { createFileRoute } from "@tanstack/react-router";
import { useFolder } from "@/features/folders/api";
import { RuleSetLists } from "@/features/rule-sets/RuleSetLists";

export const Route = createFileRoute("/_app/folders/$folderId/rule-sets")({
  component: function FolderRuleSetsRoute() {
    const { folderId } = Route.useParams();
    const folder = useFolder(folderId);
    return folder.data ? <RuleSetLists folder={folder.data} /> : null;
  },
});
