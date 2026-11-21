import { createFileRoute } from "@tanstack/react-router";
import { AuditPage } from "@/features/audit/AuditPage";
import { validateAuditSearch } from "@/features/audit/query";

// The audit explorer: every filter and the open event are typed search params (shareable links).
export const Route = createFileRoute("/_app/audit/")({
  validateSearch: validateAuditSearch,
  component: function AuditRoute() {
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <AuditPage
        search={search}
        onSearchChange={(next) => {
          void navigate({ search: next });
        }}
      />
    );
  },
});
