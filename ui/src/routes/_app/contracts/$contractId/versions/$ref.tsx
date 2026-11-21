import { createFileRoute } from "@tanstack/react-router";
import { VersionDetail } from "@/features/contracts/VersionDetail";

export const Route = createFileRoute(
  "/_app/contracts/$contractId/versions/$ref",
)({
  component: function VersionDetailRoute() {
    const { contractId, ref } = Route.useParams();
    return <VersionDetail key={ref} contractId={contractId} versionRef={ref} />;
  },
});
