// Service accounts (/admin/service-accounts, quick 261119-l30): local service accounts and registered
// Entra service principals in one table (Name, Source, Owner, Status) with the All | Local | Entra
// filter (`?source=`), a search box (`?q=`), "Hide deactivated" and one "New" menu: "Create service
// account" (name, owner) and "Register Entra service principal" (by object ID). The Entra list is
// requested only when Entra sign-in is configured and the caller holds entra:read or entra:manage
// somewhere.
import { Link, useNavigate, useRouteContext } from "@tanstack/react-router";
import { UserCog } from "lucide-react";
import { useState } from "react";
import type { EntraServicePrincipal, ServiceAccount } from "@/api/types";
import { type Column } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { useMe } from "@/features/access/useMe";
import { holdsAny, useHeldAccess } from "@/features/access/useHeldAccess";
import { useCursorList } from "@/lib/useCursorList";
import { CreateOwnedDialog } from "./CreateOwnedDialog";
import { EntraRegisterDialog } from "./EntraRegisterDialog";
import {
  MergedListView,
  localEntraMenuItems,
  NewMenu,
  ownerColumn,
  sourceColumn,
  sourcesShown,
  type Source,
  type SourceSearch,
  useMergedCursorLists,
} from "./SourceList";
import {
  ActiveBadge,
  idKeys,
  ownerOptions,
  useApi,
  useFolderLookup,
  useSettle,
} from "./shared";
import styles from "./admin.module.css";

type AccountRow = {
  key: string;
  source: Source;
  id: string;
  name: string;
  ownerFolderId: string;
  active: boolean;
  /** What the search box matches: name, owner and the Entra object ID. */
  text: string;
};

export function ServiceAccountsPage({
  search,
  onSearchChange,
}: {
  search: SourceSearch;
  onSearchChange: (next: SourceSearch) => void;
}) {
  const api = useApi();
  const settle = useSettle();
  const navigate = useNavigate();
  const me = useMe();
  const held = useHeldAccess();
  const { config } = useRouteContext({ from: "__root__" });
  const lookup = useFolderLookup();
  const localOwners = ownerOptions(
    me.data,
    lookup.folders,
    "service-account:create",
  );
  const entraOwners = ownerOptions(me.data, lookup.folders, "entra:manage");
  const [dialog, setDialog] = useState<"local" | "entra" | null>(null);
  const [hideInactive, setHideInactive] = useState(false);
  // Until the caller's access is known nothing is requested: which sources to show depends on it.
  const resolving = config.entraEnabled && held.data === undefined;
  const entraAvailable =
    config.entraEnabled &&
    held.data !== undefined &&
    holdsAny(held.data, "entra:read", "entra:manage");
  const shown = sourcesShown(search, entraAvailable);

  const localList = useCursorList<ServiceAccount>({
    queryKey: idKeys.serviceAccounts,
    enabled: !resolving && shown.local,
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/identity/service-accounts", {
        params: { query: { limit: 100, ...(cursor ? { cursor } : {}) } },
      });
      if (data === undefined)
        throw new Error("GET /service-accounts had no body");
      return data;
    },
  });
  const entraList = useCursorList<EntraServicePrincipal>({
    queryKey: idKeys.entraSps,
    enabled: entraAvailable && shown.entra,
    fetchPage: async (cursor) => {
      const { data } = await api.GET(
        "/api/v1/identity/entra/service-principals",
        { params: { query: { limit: 100, ...(cursor ? { cursor } : {}) } } },
      );
      if (data === undefined)
        throw new Error("GET /entra/service-principals had no body");
      return data;
    },
  });
  const merged = useMergedCursorLists<AccountRow>(
    [
      {
        ...localList,
        rows: localList.rows.map((account) => ({
          key: `local:${account.id}`,
          source: "local" as const,
          id: account.id,
          name: account.name,
          ownerFolderId: account.owner_folder_id,
          active: account.is_active,
          text: `${account.name} ${lookup.ownerLabel(account.owner_folder_id)}`,
        })),
        label: "Local service accounts",
        enabled: !resolving && shown.local,
      },
      {
        ...entraList,
        rows: entraList.rows.map((principal) => ({
          key: `entra:${principal.id}`,
          source: "entra" as const,
          id: principal.id,
          name: principal.display_name,
          ownerFolderId: principal.owner_folder_id,
          active: principal.is_active,
          text: `${principal.display_name} ${lookup.ownerLabel(principal.owner_folder_id)} ${principal.object_id}`,
        })),
        label: "Entra service principals",
        enabled: entraAvailable && shown.entra,
      },
    ],
    (row) => row.name,
  );
  const needle = (search.q ?? "").toLowerCase();
  const filtered = merged.rows.filter(
    (row) =>
      (needle === "" || row.text.toLowerCase().includes(needle)) &&
      (!hideInactive || row.active),
  );

  const columns: Column<AccountRow>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      sortValue: (row) => row.name,
      cell: (row) =>
        row.source === "local" ? (
          <Link
            to="/admin/service-accounts/$serviceAccountId"
            params={{ serviceAccountId: row.id }}
            className={styles.rowLink}
          >
            {row.name}
          </Link>
        ) : (
          <Link
            to="/admin/entra/service-principals/$registrationId"
            params={{ registrationId: row.id }}
            className={styles.rowLink}
          >
            {row.name}
          </Link>
        ),
    },
    sourceColumn<AccountRow>(),
    ownerColumn<AccountRow>(lookup),
    {
      id: "status",
      header: "Status",
      cell: (row) => <ActiveBadge active={row.active} />,
    },
  ];

  const menu = (
    <NewMenu
      items={localEntraMenuItems({
        localLabel: "Create service account",
        entraLabel: "Register Entra service principal",
        showLocal: localOwners.length > 0,
        showEntra: config.entraEnabled && entraOwners.length > 0,
        onPick: setDialog,
      })}
    />
  );

  return (
    <div className={styles.stack}>
      <PageHeader
        title="Service accounts"
        breadcrumbs={[{ label: "Administration" }]}
        description={
          entraAvailable
            ? "Non-human principals for pipelines: local service accounts with API keys and Entra service principals."
            : "Non-human principals for pipelines. Each holds up to two active API keys."
        }
        actions={menu}
      />
      <MergedListView
        id="service-accounts"
        plural="service accounts"
        caption="Service accounts"
        loading={resolving}
        entraAvailable={entraAvailable}
        search={search}
        onSearchChange={onSearchChange}
        hideDeactivated={{ checked: hideInactive, onChange: setHideInactive }}
        merged={merged}
        rows={filtered}
        columns={columns}
        getRowId={(row) => row.key}
        empty={{
          icon: UserCog,
          heading: "No service accounts yet",
          body: "Create one for each pipeline that calls the API.",
        }}
      />
      <CreateOwnedDialog
        open={dialog === "local"}
        onOpenChange={(open) => {
          setDialog(open ? "local" : null);
        }}
        owners={localOwners}
        idPrefix="sa"
        title="Create service account"
        description="Grant it roles on folders' Access tabs, then create a key for your pipeline."
        emptyNameMessage="Enter a name."
        conflictMessage="A service account with this name already exists here."
        create={async (body) => {
          const { data } = await api.POST("/api/v1/identity/service-accounts", {
            body,
          });
          if (data === undefined)
            throw new Error("POST /service-accounts had no body");
          return data;
        }}
        onCreated={(id) => {
          void settle(idKeys.serviceAccounts);
          void navigate({
            to: "/admin/service-accounts/$serviceAccountId",
            params: { serviceAccountId: id },
          });
        }}
      />
      <EntraRegisterDialog
        kind="service-principal"
        open={dialog === "entra"}
        onOpenChange={(open) => {
          setDialog(open ? "entra" : null);
        }}
      />
    </div>
  );
}
