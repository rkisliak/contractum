import { createFileRoute } from "@tanstack/react-router";
import {
  ContractsPage,
  type ContractsSearch,
} from "@/features/contracts/ContractsPage";

function text(value: unknown, max = 200): string | undefined {
  return typeof value === "string" && value.trim() !== ""
    ? value.trim().slice(0, max)
    : undefined;
}

// The global contract list. The top bar search navigates here with `q`.
export type { ContractsSearch };

export const Route = createFileRoute("/_app/contracts/")({
  validateSearch: (search: Record<string, unknown>): ContractsSearch => {
    const out: ContractsSearch = {};
    const q = text(search.q);
    if (q) out.q = q;
    const status = text(search.status, 40);
    if (status) out.status = status;
    const domain = text(search.domain);
    if (domain) out.domain = domain;
    const deleted = text(search.deleted, 64);
    if (deleted) out.deleted = deleted;
    const deletedName = text(search.deletedName);
    if (deletedName) out.deletedName = deletedName;
    const deletedFolder = text(search.deletedFolder, 64);
    if (deletedFolder) out.deletedFolder = deletedFolder;
    return out;
  },
  component: function ContractsRoute() {
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <ContractsPage
        search={search}
        onSearchChange={(next) => {
          void navigate({ search: next, replace: true });
        }}
      />
    );
  },
});
