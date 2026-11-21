import { createFileRoute } from "@tanstack/react-router";
import { ContractDrafts } from "@/features/contracts/ContractDrafts";

export const Route = createFileRoute("/_app/contracts/$contractId/drafts/")({
  component: function ContractDraftsRoute() {
    const { contractId } = Route.useParams();
    return <ContractDrafts contractId={contractId} />;
  },
});
