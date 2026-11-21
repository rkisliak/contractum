import { createFileRoute } from "@tanstack/react-router";
import { ServiceAccountsPage } from "@/features/admin/ServiceAccountsPage";
import { validateSourceSearch } from "@/features/admin/SourceList";

export const Route = createFileRoute("/_app/admin/service-accounts/")({
  validateSearch: validateSourceSearch,
  component: function ServiceAccountsPageRoute() {
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <ServiceAccountsPage
        search={search}
        onSearchChange={(next) => {
          void navigate({ search: next, replace: true });
        }}
      />
    );
  },
});
