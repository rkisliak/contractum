import { createFileRoute } from "@tanstack/react-router";
import {
  SETTINGS_TAB_IDS,
  SettingsPage,
  type SettingsTabId,
} from "@/features/settings/SettingsPage";

export const Route = createFileRoute("/_app/admin/settings/")({
  validateSearch: (
    search: Record<string, unknown>,
  ): { tab?: SettingsTabId } => {
    const tab = search.tab;
    return typeof tab === "string" &&
      (SETTINGS_TAB_IDS as readonly string[]).includes(tab)
      ? { tab: tab as SettingsTabId }
      : {};
  },
  component: function SettingsRoute() {
    const { tab } = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <SettingsPage
        tab={tab}
        onTabChange={(next) => {
          void navigate({ search: { tab: next }, replace: true });
        }}
      />
    );
  },
});
