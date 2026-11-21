import { createFileRoute } from "@tanstack/react-router";
import {
  NewContractPage,
  type NewContractSearch,
} from "@/features/contracts/NewContractPage";

export const Route = createFileRoute("/_app/contracts/new")({
  validateSearch: (search: Record<string, unknown>): NewContractSearch => ({
    ...(typeof search.folder === "string" && search.folder !== ""
      ? { folder: search.folder.slice(0, 64) }
      : {}),
    ...(typeof search.template === "string" && search.template !== ""
      ? { template: search.template.slice(0, 64) }
      : {}),
  }),
  component: function NewContractRoute() {
    const search = Route.useSearch();
    return <NewContractPage search={search} />;
  },
});
