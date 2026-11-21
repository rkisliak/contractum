import { createFileRoute } from "@tanstack/react-router";
import { ServiceAccountDetailPage } from "@/features/admin/ServiceAccountDetailPage";

export const Route = createFileRoute(
  "/_app/admin/service-accounts/$serviceAccountId",
)({
  component: function ServiceAccountRoute() {
    const { serviceAccountId } = Route.useParams();
    return <ServiceAccountDetailPage accountId={serviceAccountId} />;
  },
});
