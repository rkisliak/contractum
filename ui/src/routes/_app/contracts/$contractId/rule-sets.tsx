import { createFileRoute } from "@tanstack/react-router";
import { ContractRuleSets } from "@/features/contracts/ContractRuleSets";

export const Route = createFileRoute("/_app/contracts/$contractId/rule-sets")({
  component: function ContractRuleSetsRoute() {
    const { contractId } = Route.useParams();
    return <ContractRuleSets contractId={contractId} />;
  },
});
