import { createFileRoute } from "@tanstack/react-router";
import { EntraUserDetailPage } from "@/features/admin/EntraDetailPages";

export const Route = createFileRoute("/_app/admin/entra/users/$entraUserId")({
  component: function EntraUserRoute() {
    const { entraUserId } = Route.useParams();
    return <EntraUserDetailPage entraUserId={entraUserId} />;
  },
});
