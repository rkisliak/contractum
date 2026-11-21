import { createFileRoute } from "@tanstack/react-router";
import { UsersPage } from "@/features/admin/UsersPage";
import { validateSourceSearch } from "@/features/admin/SourceList";

export const Route = createFileRoute("/_app/admin/users/")({
  validateSearch: validateSourceSearch,
  component: function UsersRoute() {
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <UsersPage
        search={search}
        onSearchChange={(next) => {
          void navigate({ search: next, replace: true });
        }}
      />
    );
  },
});
