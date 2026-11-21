// Entra registration details (D-UI-12-09): a user (deactivate/reactivate), a group (delete the
// registration; the DELETE route is listed in docs/user/reference/http-api.md) and a service principal
// (deactivate/reactivate). A 404 renders NotFound.
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";
import type { EntraGroup, EntraServicePrincipal, EntraUser } from "@/api/types";
import { StatusBadge } from "@/components/Badge/status";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { orNotSet } from "@/components/DataTable";
import { NotFound } from "@/components/NotFound";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { isNotFound, problemOf } from "@/lib/unexpected";
import {
  ActiveBadge,
  formatDateTime,
  idKeys,
  useApi,
  useFolderLookup,
  useSettle,
} from "./shared";
import styles from "./admin.module.css";
import { PrincipalName } from "@/components/PrincipalName";
import { EntraGroupMembers } from "./EntraGroupMembers";

function useDetail<T>(
  key: readonly unknown[],
  fetch: () => Promise<T | undefined>,
) {
  return useQuery({
    queryKey: key,
    queryFn: async (): Promise<T> => {
      const data = await fetch();
      if (data === undefined) throw new Error("Entra detail had no body");
      return data;
    },
  });
}

function DetailFrame<T>({
  query,
  tab,
  render,
}: {
  query: ReturnType<typeof useDetail<T>>;
  tab: keyof typeof BACK;
  render: (data: T) => ReactNode;
}) {
  if (query.isError)
    return isNotFound(query.error) ? (
      <NotFound back={BACK[tab]} />
    ) : (
      <ProblemAlert
        problem={problemOf(query.error)}
        onRetry={() => void query.refetch()}
      />
    );
  if (query.data === undefined)
    return <Skeleton label="registration" rows={4} />;
  return <div className={styles.stack}>{render(query.data)}</div>;
}

// A missing registration goes back to the merged list, filtered to Entra.
const BACK = {
  users: { label: "users", to: "/admin/users?source=entra" },
  groups: { label: "groups", to: "/admin/groups?source=entra" },
  "service-principals": {
    label: "service accounts",
    to: "/admin/service-accounts?source=entra",
  },
} as const;

export function EntraUserDetailPage({ entraUserId }: { entraUserId: string }) {
  const api = useApi();
  const settle = useSettle();
  const [dialog, setDialog] = useState<"deactivate" | "reactivate" | null>(
    null,
  );
  const query = useDetail<EntraUser>(
    ["identity", "entra", "users", entraUserId],
    async () => {
      const { data } = await api.GET(
        "/api/v1/identity/entra/users/{entra_user_id}",
        {
          params: { path: { entra_user_id: entraUserId } },
        },
      );
      return data;
    },
  );
  return (
    <DetailFrame
      query={query}
      tab="users"
      render={(u) => (
        <>
          <PageHeader
            title={u.display_name}
            breadcrumbs={[
              { label: "Administration" },
              { label: "Users", to: "/admin/users" },
              { label: u.display_name },
            ]}
            badges={
              <>
                <StatusBadge status="source-entra" />
                <ActiveBadge active={u.is_active} />
              </>
            }
            actions={
              <Button
                variant={u.is_active ? "outline-danger" : "secondary"}
                size="small"
                onClick={() => {
                  setDialog(u.is_active ? "deactivate" : "reactivate");
                }}
              >
                {u.is_active ? "Deactivate user" : "Reactivate user"}
              </Button>
            }
          />
          <Card title="Details">
            <dl className={styles.facts}>
              <dt>Display name</dt>
              <dd>
                {u.display_name}
                <p className={styles.factNote}>
                  Comes from Microsoft Entra ID; change it in Microsoft.
                </p>
              </dd>
              <dt>Username</dt>
              <dd>{orNotSet(u.preferred_username)}</dd>
              <dt>Tenant ID</dt>
              <dd className={styles.ident}>{u.tenant_id}</dd>
              <dt>Object ID</dt>
              <dd className={styles.ident}>{u.object_id}</dd>
              <dt>Principal id</dt>
              <dd className={styles.ident}>{u.principal_id}</dd>
              <dt>Registered</dt>
              <dd>
                {u.registered_directly
                  ? "By an admin"
                  : "At first sign\u2011in"}
                , {formatDateTime(u.created_at)}
              </dd>
            </dl>
          </Card>
          <ConfirmDialog
            open={dialog === "deactivate"}
            onOpenChange={(open) => {
              if (!open) setDialog(null);
            }}
            title={`Deactivate ${u.display_name}?`}
            body="Their sessions end and every PAT they own is revoked. They are blocked within seconds regardless of Entra."
            objectName={u.display_name}
            confirmLabel={`Deactivate ${u.display_name}`}
            typeToConfirm={u.display_name}
            busyLabel="Deactivating…"
            onConfirm={async () => {
              await api.POST(
                "/api/v1/identity/entra/users/{entra_user_id}/deactivate",
                { params: { path: { entra_user_id: entraUserId } } },
              );
              await settle(idKeys.entraUsers);
            }}
          />
          <ConfirmDialog
            open={dialog === "reactivate"}
            onOpenChange={(open) => {
              if (!open) setDialog(null);
            }}
            title={`Reactivate ${u.display_name}?`}
            body="They can sign in again and their grants apply again. Revoked tokens stay revoked."
            objectName={u.display_name}
            confirmLabel={`Reactivate ${u.display_name}`}
            busyLabel="Reactivating…"
            onConfirm={async () => {
              await api.POST(
                "/api/v1/identity/entra/users/{entra_user_id}/reactivate",
                { params: { path: { entra_user_id: entraUserId } } },
              );
              await settle(idKeys.entraUsers);
            }}
          />
        </>
      )}
    />
  );
}

export function EntraGroupDetailPage({
  registrationId,
}: {
  registrationId: string;
}) {
  const api = useApi();
  const settle = useSettle();
  const navigate = useNavigate();
  const lookup = useFolderLookup();
  const [deleting, setDeleting] = useState(false);
  const query = useDetail<EntraGroup>(
    ["identity", "entra", "groups", registrationId],
    async () => {
      const { data } = await api.GET(
        "/api/v1/identity/entra/groups/{registration_id}",
        { params: { path: { registration_id: registrationId } } },
      );
      return data;
    },
  );
  return (
    <DetailFrame
      query={query}
      tab="groups"
      render={(g) => (
        <>
          <PageHeader
            title={g.display_name}
            breadcrumbs={[
              { label: "Administration" },
              { label: "Groups", to: "/admin/groups" },
              { label: g.display_name },
            ]}
            badges={<StatusBadge status="source-entra" />}
            actions={
              <Button
                variant="outline-danger"
                size="small"
                onClick={() => {
                  setDeleting(true);
                }}
              >
                Delete registration
              </Button>
            }
          />
          <Card title="Details">
            <dl className={styles.facts}>
              <dt>Owner</dt>
              <dd>{lookup.ownerLabel(g.owner_folder_id)}</dd>
              <dt>Tenant ID</dt>
              <dd className={styles.ident}>{g.tenant_id}</dd>
              <dt>Object ID</dt>
              <dd className={styles.ident}>{g.object_id}</dd>
              <dt>Registered</dt>
              <dd>
                {formatDateTime(g.created_at)} by{" "}
                <PrincipalName id={g.created_by} />
              </dd>
            </dl>
          </Card>
          <EntraGroupMembers registrationId={registrationId} />
          <ConfirmDialog
            open={deleting}
            onOpenChange={setDeleting}
            title={`Delete the registration of ${g.display_name}?`}
            body="Admission through this group ends, so members not registered another way can no longer sign in, and every grant made to this group is removed in the same step."
            objectName="registration"
            confirmLabel="Delete registration"
            typeToConfirm={g.display_name}
            verb="Delete"
            busyLabel="Deleting…"
            onConfirm={async () => {
              await api.DELETE(
                "/api/v1/identity/entra/groups/{registration_id}",
                {
                  params: { path: { registration_id: registrationId } },
                },
              );
              await settle(idKeys.entraGroups, ["identity", "grants"]);
              void navigate({
                to: "/admin/groups",
                search: { source: "entra" },
              });
            }}
          />
        </>
      )}
    />
  );
}

export function EntraServicePrincipalDetailPage({
  registrationId,
}: {
  registrationId: string;
}) {
  const api = useApi();
  const settle = useSettle();
  const lookup = useFolderLookup();
  const [dialog, setDialog] = useState<"deactivate" | "reactivate" | null>(
    null,
  );
  const query = useDetail<EntraServicePrincipal>(
    ["identity", "entra", "service-principals", registrationId],
    async () => {
      const { data } = await api.GET(
        "/api/v1/identity/entra/service-principals/{registration_id}",
        { params: { path: { registration_id: registrationId } } },
      );
      return data;
    },
  );
  return (
    <DetailFrame
      query={query}
      tab="service-principals"
      render={(sp) => (
        <>
          <PageHeader
            title={sp.display_name}
            breadcrumbs={[
              { label: "Administration" },
              { label: "Service accounts", to: "/admin/service-accounts" },
              { label: sp.display_name },
            ]}
            badges={
              <>
                <StatusBadge status="source-entra" />
                <ActiveBadge active={sp.is_active} />
              </>
            }
            actions={
              <Button
                variant={sp.is_active ? "outline-danger" : "secondary"}
                size="small"
                onClick={() => {
                  setDialog(sp.is_active ? "deactivate" : "reactivate");
                }}
              >
                {sp.is_active ? "Deactivate" : "Reactivate"}
              </Button>
            }
          />
          <Card title="Details">
            <dl className={styles.facts}>
              <dt>Owner</dt>
              <dd>{lookup.ownerLabel(sp.owner_folder_id)}</dd>
              <dt>Tenant ID</dt>
              <dd className={styles.ident}>{sp.tenant_id}</dd>
              <dt>Object ID</dt>
              <dd className={styles.ident}>{sp.object_id}</dd>
              <dt>Principal id</dt>
              <dd className={styles.ident}>{sp.principal_id}</dd>
              <dt>Registered</dt>
              <dd>{formatDateTime(sp.created_at)}</dd>
            </dl>
          </Card>
          <ConfirmDialog
            open={dialog === "deactivate"}
            onOpenChange={(open) => {
              if (!open) setDialog(null);
            }}
            title={`Deactivate ${sp.display_name}?`}
            body="Its tokens are refused from now on, regardless of Entra. Its grants stay and apply again on reactivation."
            objectName={sp.display_name}
            confirmLabel={`Deactivate ${sp.display_name}`}
            busyLabel="Deactivating…"
            onConfirm={async () => {
              await api.POST(
                "/api/v1/identity/entra/service-principals/{registration_id}/deactivate",
                { params: { path: { registration_id: registrationId } } },
              );
              await settle(idKeys.entraSps);
            }}
          />
          <ConfirmDialog
            open={dialog === "reactivate"}
            onOpenChange={(open) => {
              if (!open) setDialog(null);
            }}
            title={`Reactivate ${sp.display_name}?`}
            body="Its tokens are accepted again and its grants apply again."
            objectName={sp.display_name}
            confirmLabel={`Reactivate ${sp.display_name}`}
            busyLabel="Reactivating…"
            onConfirm={async () => {
              await api.POST(
                "/api/v1/identity/entra/service-principals/{registration_id}/reactivate",
                { params: { path: { registration_id: registrationId } } },
              );
              await settle(idKeys.entraSps);
            }}
          />
        </>
      )}
    />
  );
}
