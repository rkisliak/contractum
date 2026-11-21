import { createFileRoute } from "@tanstack/react-router";
import { UserDetailPage } from "@/features/admin/UserDetailPage";

export const Route = createFileRoute("/_app/admin/users/$userId")({
  component: function UserDetailRoute() {
    const { userId } = Route.useParams();
    return <UserDetailPage userId={userId} />;
  },
});
