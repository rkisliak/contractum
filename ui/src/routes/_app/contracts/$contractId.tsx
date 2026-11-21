import { createFileRoute, Outlet } from "@tanstack/react-router";
import { ContractLayout } from "@/features/contracts/ContractLayout";

// The contract layout: header and tab navigation around the child routes (index = Overview,
// document, drafts, drafts/$draftId, versions, versions/$ref, diff, rule-sets, subscribers).
export const Route = createFileRoute("/_app/contracts/$contractId")({
  component: function ContractRoute() {
    const { contractId } = Route.useParams();
    return (
      <ContractLayout contractId={contractId}>
        <Outlet />
      </ContractLayout>
    );
  },
});
