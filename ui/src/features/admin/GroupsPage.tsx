// Groups (/admin/groups, quick 261119-l30): local groups and registered Microsoft Entra groups in one
// table (Name, Source, Owner, Members) with the All | Local | Entra filter (`?source=`), a search box
// (`?q=`) and one "New" menu: "Create local group" (name, owner) and "Register Entra group" (by object
// ID). A local row counts its members (one capped request per row through a limiter); an Entra
// group's members are managed in Entra. The Entra list is requested only when Entra sign-in is
// configured and the caller holds entra:read or entra:manage somewhere.
import { Link, useNavigate, useRouteContext } from "@tanstack/react-router";
import { UsersRound } from "lucide-react";
import { useState } from "react";
import type { EntraGroup, LocalGroup } from "@/api/types";
import { type Column } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { useMe } from "@/features/access/useMe";
import { holdsAny, useHeldAccess } from "@/features/access/useHeldAccess";
import { useCursorList } from "@/lib/useCursorList";
import { CreateOwnedDialog } from "./CreateOwnedDialog";
import { EntraRegisterDialog } from "./EntraRegisterDialog";
import {
  MemberCount,
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
  idKeys,
  ownerOptions,
  useApi,
  useFolderLookup,
  useSettle,
} from "./shared";
import styles from "./admin.module.css";

type GroupRow = {
  key: string;
  source: Source;
  id: string;
  name: string;
  ownerFolderId: string;
  /** What the search box matches: name, owner and the Entra object ID. */
  text: string;
};

export function GroupsPage({
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
  const localOwners = ownerOptions(me.data, lookup.folders, "group:create");
  const entraOwners = ownerOptions(me.data, lookup.folders, "entra:manage");
  const [dialog, setDialog] = useState<"local" | "entra" | null>(null);
  // The Entra groups list needs the baseline entra:list, but the screen is for those who manage
  // Entra registrations: entra:read or entra:manage held anywhere.
  // Until the caller's access is known nothing is requested: which sources to show depends on it.
  const resolving = config.entraEnabled && held.data === undefined;
  const entraAvailable =
    config.entraEnabled &&
    held.data !== undefined &&
    holdsAny(held.data, "entra:read", "entra:manage");
  const shown = sourcesShown(search, entraAvailable);

  const localList = useCursorList<LocalGroup>({
    queryKey: idKeys.groups,
    enabled: !resolving && shown.local,
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/identity/groups", {
        params: { query: { limit: 100, ...(cursor ? { cursor } : {}) } },
      });
      if (data === undefined) throw new Error("GET /groups had no body");
      return data;
    },
  });
  const entraList = useCursorList<EntraGroup>({
    queryKey: idKeys.entraGroups,
    enabled: entraAvailable && shown.entra,
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/identity/entra/groups", {
        params: { query: { limit: 100, ...(cursor ? { cursor } : {}) } },
      });
      if (data === undefined) throw new Error("GET /entra/groups had no body");
      return data;
    },
  });
  const merged = useMergedCursorLists<GroupRow>(
    [
      {
        ...localList,
        rows: localList.rows.map((group) => ({
          key: `local:${group.id}`,
          source: "local" as const,
          id: group.id,
          name: group.name,
          ownerFolderId: group.owner_folder_id,
          text: `${group.name} ${lookup.ownerLabel(group.owner_folder_id)}`,
        })),
        label: "Local groups",
        enabled: !resolving && shown.local,
      },
      {
        ...entraList,
        rows: entraList.rows.map((group) => ({
          key: `entra:${group.id}`,
          source: "entra" as const,
          id: group.id,
          name: group.display_name,
          ownerFolderId: group.owner_folder_id,
          text: `${group.display_name} ${lookup.ownerLabel(group.owner_folder_id)} ${group.object_id}`,
        })),
        label: "Entra groups",
        enabled: entraAvailable && shown.entra,
      },
    ],
    (row) => row.name,
  );
  const needle = (search.q ?? "").toLowerCase();
  const filtered = merged.rows.filter(
    (row) => needle === "" || row.text.toLowerCase().includes(needle),
  );

  const columns: Column<GroupRow>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      sortValue: (row) => row.name,
      cell: (row) =>
        row.source === "local" ? (
          <Link
            to="/admin/groups/$groupId"
            params={{ groupId: row.id }}
            className={styles.rowLink}
          >
            {row.name}
          </Link>
        ) : (
          <Link
            to="/admin/entra/groups/$registrationId"
            params={{ registrationId: row.id }}
            className={styles.rowLink}
          >
            {row.name}
          </Link>
        ),
    },
    sourceColumn<GroupRow>(),
    ownerColumn<GroupRow>(lookup),
    {
      id: "members",
      header: "Members",
      cell: (row) =>
        row.source === "local" ? (
          <MemberCount groupId={row.id} />
        ) : (
          <span className={styles.muted}>Managed in Entra</span>
        ),
    },
  ];

  const menu = (
    <NewMenu
      items={localEntraMenuItems({
        localLabel: "Create local group",
        entraLabel: "Register Entra group",
        showLocal: localOwners.length > 0,
        showEntra: config.entraEnabled && entraOwners.length > 0,
        onPick: setDialog,
      })}
    />
  );

  return (
    <div className={styles.stack}>
      <PageHeader
        title="Groups"
        breadcrumbs={[{ label: "Administration" }]}
        description={
          entraAvailable
            ? "Local groups and registered Microsoft Entra groups. Grant a group a role on a folder's Access tab."
            : "Local groups and their members. Grant a group a role on a folder's Access tab."
        }
        actions={menu}
      />
      <MergedListView
        id="groups"
        plural="groups"
        caption="Groups"
        loading={resolving}
        entraAvailable={entraAvailable}
        search={search}
        onSearchChange={onSearchChange}
        merged={merged}
        rows={filtered}
        columns={columns}
        getRowId={(row) => row.key}
        empty={{
          icon: UsersRound,
          heading: "No groups yet",
          body: "Groups collect users and service accounts so one grant covers them all.",
        }}
      />
      <CreateOwnedDialog
        open={dialog === "local"}
        onOpenChange={(open) => {
          setDialog(open ? "local" : null);
        }}
        owners={localOwners}
        idPrefix="group"
        title="Create local group"
        description="Groups owned by an Organization can only hold grants inside it."
        emptyNameMessage="Enter a group name."
        conflictMessage="A group with this name already exists here."
        create={async (body) => {
          const { data } = await api.POST("/api/v1/identity/groups", { body });
          if (data === undefined) throw new Error("POST /groups had no body");
          return data;
        }}
        onCreated={(id) => {
          void settle(idKeys.groups);
          void navigate({
            to: "/admin/groups/$groupId",
            params: { groupId: id },
          });
        }}
      />
      <EntraRegisterDialog
        kind="group"
        open={dialog === "entra"}
        onOpenChange={(open) => {
          setDialog(open ? "entra" : null);
        }}
      />
    </div>
  );
}
