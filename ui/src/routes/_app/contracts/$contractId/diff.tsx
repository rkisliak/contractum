import { createFileRoute } from "@tanstack/react-router";
import {
  ContractDiff,
  type DiffSearch,
} from "@/features/contracts/ContractDiff";

export const Route = createFileRoute("/_app/contracts/$contractId/diff")({
  validateSearch: (search: Record<string, unknown>): DiffSearch => ({
    ...(typeof search.from === "string" && search.from !== ""
      ? { from: search.from.slice(0, 200) }
      : {}),
    ...(typeof search.to === "string" && search.to !== ""
      ? { to: search.to.slice(0, 200) }
      : {}),
  }),
  component: function ContractDiffRoute() {
    const { contractId } = Route.useParams();
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <ContractDiff
        contractId={contractId}
        search={search}
        onSearchChange={(next) => {
          void navigate({ search: next, replace: true });
        }}
      />
    );
  },
});
