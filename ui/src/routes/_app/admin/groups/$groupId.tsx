import { createFileRoute } from "@tanstack/react-router";
import { GroupDetailPage } from "@/features/admin/GroupDetailPage";

export const Route = createFileRoute("/_app/admin/groups/$groupId")({
  component: function GroupDetailRoute() {
    const { groupId } = Route.useParams();
    return <GroupDetailPage groupId={groupId} />;
  },
});
