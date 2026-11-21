import { createFileRoute } from "@tanstack/react-router";
import { ContractSubscribers } from "@/features/contracts/ContractSubscribers";

export const Route = createFileRoute("/_app/contracts/$contractId/subscribers")(
  {
    component: function ContractSubscribersRoute() {
      const { contractId } = Route.useParams();
      return <ContractSubscribers contractId={contractId} />;
    },
  },
);
