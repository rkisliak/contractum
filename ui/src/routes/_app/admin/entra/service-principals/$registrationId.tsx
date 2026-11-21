import { createFileRoute } from "@tanstack/react-router";
import { EntraServicePrincipalDetailPage } from "@/features/admin/EntraDetailPages";

export const Route = createFileRoute(
  "/_app/admin/entra/service-principals/$registrationId",
)({
  component: function EntraServicePrincipalRoute() {
    const { registrationId } = Route.useParams();
    return <EntraServicePrincipalDetailPage registrationId={registrationId} />;
  },
});
