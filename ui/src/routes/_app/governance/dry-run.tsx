import { createFileRoute } from "@tanstack/react-router";
import {
  DryRunPage,
  type DryRunSearch,
} from "@/features/governance/DryRunPage";

export const Route = createFileRoute("/_app/governance/dry-run")({
  validateSearch: (search: Record<string, unknown>): DryRunSearch => {
    const version =
      search.version === "draft"
        ? ("draft" as const)
        : Number.isInteger(Number(search.version)) && Number(search.version) > 0
          ? Number(search.version)
          : undefined;
    return {
      ...(typeof search.rule_set_id === "string" && search.rule_set_id !== ""
        ? { rule_set_id: search.rule_set_id }
        : {}),
      ...(version !== undefined ? { version } : {}),
      ...(typeof search.folder_id === "string" && search.folder_id !== ""
        ? { folder_id: search.folder_id }
        : {}),
    };
  },
  component: function DryRunRoute() {
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <DryRunPage
        search={search}
        onSearchChange={(next) => {
          void navigate({ search: next, replace: true });
        }}
      />
    );
  },
});
