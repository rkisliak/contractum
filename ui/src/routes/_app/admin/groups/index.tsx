import { createFileRoute } from "@tanstack/react-router";
import { GroupsPage } from "@/features/admin/GroupsPage";
import { validateSourceSearch } from "@/features/admin/SourceList";

export const Route = createFileRoute("/_app/admin/groups/")({
  validateSearch: validateSourceSearch,
  component: function GroupsPageRoute() {
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <GroupsPage
        search={search}
        onSearchChange={(next) => {
          void navigate({ search: next, replace: true });
        }}
      />
    );
  },
});
