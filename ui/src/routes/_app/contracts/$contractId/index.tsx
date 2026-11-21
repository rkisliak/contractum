import { createFileRoute } from "@tanstack/react-router";
import { ContractOverview } from "@/features/contracts/ContractOverview";

export const Route = createFileRoute("/_app/contracts/$contractId/")({
  component: function ContractOverviewRoute() {
    const { contractId } = Route.useParams();
    return <ContractOverview contractId={contractId} />;
  },
});
