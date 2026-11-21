// "Versions" (the rail's info button and the account menu): which build of each component is
// running, so a bug report can name it. The UI's own build comes from vite.config.ts; each routed
// service answers its public `GET /api/v1/<module>/version`. A service that does not answer reads
// "Unavailable" (muted), never an error alert. Mail and the gateway are not reachable from the
// browser, so they are not listed.
import { useQueries } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import type { ServiceVersion } from "@/api/types";
import { Button } from "@/components/Button";
import { DataTable, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { Identifier } from "@/components/Identifier";
import { uiBuild } from "@/buildInfo";
import styles from "./VersionsDialog.module.css";

export const VERSIONED_SERVICES = [
  "identity",
  "contracts",
  "validation",
  "audit",
] as const;
type Service = (typeof VERSIONED_SERVICES)[number];

const LABELS: Record<Service | "ui", string> = {
  ui: "Web UI",
  identity: "Identity",
  contracts: "Contracts",
  validation: "Validation",
  audit: "Audit",
};

type Row = {
  id: string;
  label: string;
  state: "ok" | "loading" | "unavailable";
  version?: string;
  commit?: string;
  builtAt?: string;
};

function formatBuilt(value: string | undefined): string {
  if (!value || value === "dev") return value ?? "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      });
}

export function VersionsDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { api } = useRouteContext({ from: "__root__" });
  const results = useQueries({
    queries: VERSIONED_SERVICES.map((service) => ({
      queryKey: ["versions", service],
      enabled: open,
      staleTime: 60_000,
      retry: false,
      queryFn: async (): Promise<ServiceVersion> => {
        const { data } = await api.GET(`/api/v1/${service}/version`);
        if (data === undefined) throw new Error("no version body");
        return data;
      },
    })),
  });
  const rows: Row[] = [
    {
      id: "ui",
      label: LABELS.ui,
      state: "ok",
      version: uiBuild.version,
      commit: uiBuild.commit,
      builtAt: uiBuild.builtAt,
    },
    ...VERSIONED_SERVICES.map((service, index): Row => {
      const query = results[index];
      if (query?.data)
        return {
          id: service,
          label: LABELS[service],
          state: "ok",
          version: query.data.version,
          commit: query.data.commit,
          builtAt: query.data.built_at,
        };
      return {
        id: service,
        label: LABELS[service],
        state: query?.isError ? "unavailable" : "loading",
      };
    }),
  ];
  const columns: Column<Row>[] = [
    {
      id: "component",
      header: "Component",
      rowHeader: true,
      cell: (r) => r.label,
    },
    {
      id: "version",
      header: "Version",
      cell: (r) =>
        r.state === "ok" ? (
          r.version
        ) : (
          <span className={styles.muted}>
            {r.state === "loading" ? "Loading…" : "Unavailable"}
          </span>
        ),
    },
    {
      id: "commit",
      header: "Commit",
      cell: (r) =>
        r.state === "ok" && r.commit ? <Identifier value={r.commit} /> : null,
    },
    {
      id: "built",
      header: "Built",
      cell: (r) => (r.state === "ok" ? formatBuilt(r.builtAt) : null),
    },
  ];
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size={512}
      title="Versions"
      description="The build of each component you are using. Include it when you report a problem."
      footer={
        <Button
          variant="secondary"
          size="small"
          onClick={() => {
            onOpenChange(false);
          }}
        >
          Close
        </Button>
      }
    >
      <DataTable
        caption="Component versions"
        captionHidden
        compact
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
      />
    </Dialog>
  );
}
