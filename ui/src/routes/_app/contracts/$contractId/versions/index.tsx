import { createFileRoute } from "@tanstack/react-router";
import {
  ContractVersions,
  type VersionsSearch,
} from "@/features/contracts/ContractVersions";

export const Route = createFileRoute("/_app/contracts/$contractId/versions/")({
  validateSearch: (search: Record<string, unknown>): VersionsSearch => ({
    ...(typeof search.published === "string"
      ? { published: search.published.slice(0, 200) }
      : {}),
    ...(typeof search.status === "string"
      ? { status: search.status.slice(0, 20) }
      : {}),
    ...(typeof search.warnings === "number"
      ? { warnings: search.warnings }
      : {}),
  }),
  component: function ContractVersionsRoute() {
    const { contractId } = Route.useParams();
    const search = Route.useSearch();
    return <ContractVersions contractId={contractId} search={search} />;
  },
});
