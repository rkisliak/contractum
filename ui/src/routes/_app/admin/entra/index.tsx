import { createFileRoute, redirect } from "@tanstack/react-router";

// /admin/entra is gone: Users, Groups and Service accounts list Entra records beside the local
// ones. Old bookmarks (`?tab=`) land on the matching merged page filtered to Entra. The targets
// are three fixed internal routes, so the redirect can never leave the app (T-261119-l30-03).
type EntraTab = "groups" | "service-principals";

export const Route = createFileRoute("/_app/admin/entra/")({
  validateSearch: (search: Record<string, unknown>): { tab?: EntraTab } =>
    search.tab === "groups" || search.tab === "service-principals"
      ? { tab: search.tab }
      : {},
  beforeLoad: ({ search }) => {
    if (search.tab === "groups")
      throw redirect({
        to: "/admin/groups",
        search: { source: "entra" },
        replace: true,
      });
    if (search.tab === "service-principals")
      throw redirect({
        to: "/admin/service-accounts",
        search: { source: "entra" },
        replace: true,
      });
    throw redirect({
      to: "/admin/users",
      search: { source: "entra" },
      replace: true,
    });
  },
});
