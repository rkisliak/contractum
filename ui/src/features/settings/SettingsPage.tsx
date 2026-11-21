// Admin > Settings (quick 261120-0yc, D-16): one page, one tab per settings section that the API
// returns, in the fixed registry order. A tab shows only when its section exists, so a later plan
// adds a section and one registry entry. Export and Import are page-level actions. Only a caller
// that holds `settings:read` sees any of it; everyone else gets NotAvailable and no request.
import { useQueryClient } from "@tanstack/react-query";
import { Download, Upload } from "lucide-react";
import { useState, type ReactNode } from "react";
import type { SettingsOut, SettingsSection } from "@/api/types";
import { Button } from "@/components/Button";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { NotAvailable, Skeleton } from "@/components/states";
import { Tabs } from "@/components/Tabs";
import { hasGlobal, useMe } from "@/features/access/useMe";
import { problemOf } from "@/lib/unexpected";
import { settingsKeys, useExportSettings, useSettings } from "./api";
import { EntraTab } from "./EntraTab";
import { GovernanceTab } from "./GovernanceTab";
import { ImportSettingsDialog } from "./ImportSettingsDialog";
import { NotificationsTab } from "./NotificationsTab";
import { PlatformTab } from "./PlatformTab";
import { SecurityTab } from "./SecurityTab";

export const SETTINGS_TAB_IDS = [
  "entra",
  "notifications",
  "governance",
  "security",
  "platform",
] as const;
export type SettingsTabId = (typeof SETTINGS_TAB_IDS)[number];

type TabProps = {
  section: SettingsSection;
  choices: SettingsOut["choices"];
  onReload: () => void;
};

/** The fixed tab order. A tab is drawn only when its section is returned and it has a screen. */
const TAB_REGISTRY: {
  id: SettingsTabId;
  label: string;
  render: (props: TabProps) => ReactNode;
}[] = [
  { id: "entra", label: "Entra", render: (p) => <EntraTab {...p} /> },
  {
    id: "notifications",
    label: "Notifications",
    render: (p) => <NotificationsTab {...p} />,
  },
  {
    id: "governance",
    label: "Governance",
    render: (p) => <GovernanceTab {...p} />,
  },
  {
    id: "security",
    label: "Sign-in & security",
    render: (p) => <SecurityTab {...p} />,
  },
  { id: "platform", label: "Platform", render: (p) => <PlatformTab {...p} /> },
];

export function SettingsPage({
  tab,
  onTabChange,
}: {
  tab: SettingsTabId | undefined;
  onTabChange: (tab: SettingsTabId) => void;
}) {
  const me = useMe();
  const settings = useSettings();
  const exportFile = useExportSettings();
  const queryClient = useQueryClient();
  const [importOpen, setImportOpen] = useState(false);
  const header = (actions?: ReactNode) => (
    <PageHeader
      title="Settings"
      breadcrumbs={[{ label: "Administration" }]}
      description="Platform settings a Global admin changes without a redeploy. Helm values stay the defaults."
      actions={actions}
    />
  );

  if (me.data !== undefined && !hasGlobal(me.data, "settings:read")) {
    return (
      <>
        {header()}
        <NotAvailable what="Settings" />
      </>
    );
  }
  if (settings.isError) {
    return (
      <>
        {header()}
        <ProblemAlert
          problem={problemOf(settings.error)}
          onRetry={() => void settings.refetch()}
        />
      </>
    );
  }
  if (settings.data === undefined) {
    return (
      <>
        {header()}
        <Skeleton label="settings" rows={4} />
      </>
    );
  }
  const data = settings.data;
  const bySection = new Map(data.sections.map((s) => [s.section, s]));
  const tabs = TAB_REGISTRY.flatMap((entry) => {
    const section = bySection.get(entry.id);
    return section === undefined ? [] : [{ entry, section }];
  });
  const active = tabs.find((t) => t.entry.id === tab) ?? tabs[0];
  const reload = () => {
    void queryClient.invalidateQueries({ queryKey: settingsKeys.all });
  };

  return (
    <>
      {header(
        <>
          <Button
            variant="outline-neutral"
            size="small"
            icon={Download}
            busy={exportFile.isPending}
            busyLabel="Exporting…"
            onClick={() => {
              exportFile.mutate();
            }}
          >
            Export
          </Button>
          <Button
            variant="outline-neutral"
            size="small"
            icon={Upload}
            onClick={() => {
              setImportOpen(true);
            }}
          >
            Import
          </Button>
        </>,
      )}
      {exportFile.isError ? (
        <ProblemAlert problem={problemOf(exportFile.error)} context="action" />
      ) : null}
      {active === undefined ? (
        <NotAvailable what="Settings" />
      ) : (
        <Tabs
          label="Settings sections"
          value={active.entry.id}
          onValueChange={(next) => {
            onTabChange(next as SettingsTabId);
          }}
          items={tabs.map(({ entry, section }) => ({
            value: entry.id,
            label: entry.label,
            content: entry.render({
              section,
              choices: data.choices,
              onReload: reload,
            }),
          }))}
        />
      )}
      <ImportSettingsDialog open={importOpen} onOpenChange={setImportOpen} />
    </>
  );
}
