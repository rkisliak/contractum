import { createFileRoute } from "@tanstack/react-router";
import { ContractDocument } from "@/features/contracts/ContractDocument";

export const Route = createFileRoute("/_app/contracts/$contractId/document")({
  component: function ContractDocumentRoute() {
    const { contractId } = Route.useParams();
    return <ContractDocument contractId={contractId} />;
  },
});
