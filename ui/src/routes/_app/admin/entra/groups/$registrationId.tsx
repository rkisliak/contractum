import { createFileRoute } from "@tanstack/react-router";
import { EntraGroupDetailPage } from "@/features/admin/EntraDetailPages";

export const Route = createFileRoute(
  "/_app/admin/entra/groups/$registrationId",
)({
  component: function EntraGroupRoute() {
    const { registrationId } = Route.useParams();
    return <EntraGroupDetailPage registrationId={registrationId} />;
  },
});
