// Personal access tokens (D-UI-12-11). "Access tokens" (/account/tokens): the caller's own PATs,
// create (name, expiry default 30 days, Read-only) with the show-once dialog, and revoke. "All
// access tokens" (/admin/pats, `pat:read-all`): every PAT with its owner, metadata only, and
// revoke-any through the admin endpoint.
import { useState } from "react";
import type { AdminPat, Pat } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { SelectField } from "@/components/form";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { SecretRevealDialog } from "@/components/SecretRevealDialog";
import { EmptyState, Skeleton, NotAvailable } from "@/components/states";
import { TabNav } from "@/components/Tabs";
import { hasGlobal, useMe } from "@/features/access/useMe";
import { useCursorList, type CursorPage } from "@/lib/useCursorList";
import { problemOf } from "@/lib/unexpected";
import { KeyRound } from "lucide-react";
import { CreateCredentialDialog } from "./CreateCredentialDialog";
import {
  CredentialStatus,
  formatDateTime,
  idKeys,
  RowFilter,
  useApi,
  useRowFilter,
  useSettle,
} from "./shared";
import { usePrincipalNames } from "./usePrincipalNames";
import styles from "./admin.module.css";

/** Account settings sections (password and tokens). */
export function AccountTabs() {
  return (
    <TabNav
      label="Account settings"
      items={[
        { to: "/account/profile", label: "Profile" },
        { to: "/account/password", label: "Password" },
        { to: "/account/tokens", label: "Access tokens" },
      ]}
    />
  );
}

function patColumns<T extends Pat>(
  onRevoke: (row: T) => void,
  extra: Column<T>[] = [],
): Column<T>[] {
  return [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      sortValue: (row) => row.name,
      cell: (row) => row.name,
    },
    ...extra,
    {
      // Access and status side by side in one cell, on one line.
      id: "access",
      header: "Access and status",
      nowrap: true,
      cell: (row) => (
        <div className={styles.badgeRow}>
          {row.read_only ? (
            <Badge intent="neutral">Read-only</Badge>
          ) : (
            <Badge intent="brand">Read and write</Badge>
          )}
          <CredentialStatus status={row.status} />
        </div>
      ),
    },
    {
      id: "expires",
      header: "Expires",
      sortValue: (row) => row.expires_at,
      cell: (row) => formatDateTime(row.expires_at),
    },
    {
      id: "used",
      header: "Last used",
      cell: (row) =>
        row.last_used_at ? formatDateTime(row.last_used_at) : "Never",
    },
    {
      id: "actions",
      header: "Actions",
      align: "end",
      cell: (row) =>
        row.status === "active" ? (
          <Button
            variant="ghost"
            size="small"
            aria-label={`Revoke ${row.name}`}
            onClick={() => {
              onRevoke(row);
            }}
          >
            Revoke
          </Button>
        ) : null,
    },
  ];
}

/** One page of `GET /pats`: the rows plus the caller's active count and the effective limit. */
type PatPage = CursorPage<Pat> & { active_count: number; max_active: number };

export function MyTokensPage() {
  const api = useApi();
  const settle = useSettle();
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<Pat | null>(null);
  const list = useCursorList<Pat, PatPage>({
    queryKey: idKeys.myPats,
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/identity/pats", {
        params: { query: { limit: 100, ...(cursor ? { cursor } : {}) } },
      });
      if (data === undefined) throw new Error("GET /pats had no body");
      return data;
    },
  });
  // The caller's active count and the effective limit ride on every page of the list
  // (quick 261120-lbz, D-03); the first page is the freshest after a create or a revoke.
  const first = list.firstPage;
  const usage =
    first !== undefined &&
    typeof first.active_count === "number" &&
    typeof first.max_active === "number"
      ? { active: first.active_count, max: first.max_active }
      : undefined;
  const atCap = usage !== undefined && usage.active >= usage.max;
  const createButton = (
    <Button
      variant="primary"
      size="small"
      icon={KeyRound}
      disabled={atCap}
      onClick={() => {
        setCreating(true);
      }}
    >
      Create token
    </Button>
  );

  return (
    <div className={styles.stack}>
      <PageHeader
        title="Access tokens"
        description="Personal access tokens let scripts and pipelines call the API as you. Each token is shown once."
        breadcrumbs={[
          { label: "Account settings" },
          { label: "Access tokens" },
        ]}
        actions={list.rows.length > 0 ? createButton : null}
      />
      <AccountTabs />
      {usage !== undefined ? (
        <p className={styles.muted} data-testid="token-usage">
          {String(usage.active)} of {String(usage.max)} active
          {atCap
            ? `. You have reached the limit of ${String(usage.max)} active access tokens. Revoke one to create another.`
            : null}
        </p>
      ) : null}
      {list.error ? (
        <ProblemAlert problem={problemOf(list.error)} onRetry={list.refetch} />
      ) : list.isPending ? (
        <Skeleton label="access tokens" rows={3} />
      ) : list.rows.length === 0 ? (
        <EmptyState
          icon={KeyRound}
          heading="No access tokens yet"
          body="Create a token to call the API from a script or to test contracts locally."
          action={createButton}
        />
      ) : (
        <DataTable
          caption="Your access tokens"
          captionHidden
          columns={patColumns<Pat>(setRevoking)}
          rows={list.rows}
          getRowId={(row) => row.id}
          loadMore={{
            hasMore: list.hasMore,
            isLoadingMore: list.isLoadingMore,
            onLoadMore: list.loadMore,
            error: list.pageError,
          }}
        />
      )}
      <CreateCredentialDialog
        open={creating}
        onOpenChange={setCreating}
        title="Create token"
        description="The token acts with your access. It is shown once."
        submitLabel="Create token"
        defaultDays={30}
        withReadOnly
        create={async (input) => {
          const { data } = await api.POST("/api/v1/identity/pats", {
            body: {
              name: input.name,
              expires_at: input.expiresAt,
              read_only: input.readOnly,
            },
          });
          if (data === undefined) throw new Error("POST /pats had no body");
          void settle(idKeys.myPats, idKeys.allPats);
          return data.token;
        }}
        onCreated={setSecret}
      />
      <SecretRevealDialog
        open={secret !== null}
        kind="token"
        secret={secret ?? ""}
        onClosed={() => {
          setSecret(null);
        }}
      />
      {revoking ? (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setRevoking(null);
          }}
          title={`Revoke ${revoking.name}?`}
          body="Anything using this token is refused from now on. This cannot be undone."
          objectName={revoking.name}
          confirmLabel={`Revoke ${revoking.name}`}
          busyLabel="Revoking…"
          onConfirm={async () => {
            await api.DELETE("/api/v1/identity/pats/{credential_id}", {
              params: { path: { credential_id: revoking.id } },
            });
            await settle(idKeys.myPats, idKeys.allPats);
          }}
        />
      ) : null}
    </div>
  );
}

const STATUS_OPTIONS = [
  { value: "", label: "Any status" },
  { value: "active", label: "Active" },
  { value: "expired", label: "Expired" },
  { value: "revoked", label: "Revoked" },
];

export function AllPatsPage() {
  const api = useApi();
  const me = useMe();
  const settle = useSettle();
  const nameOf = usePrincipalNames();
  const [status, setStatus] = useState<"" | "active" | "expired" | "revoked">(
    "",
  );
  const [revoking, setRevoking] = useState<AdminPat | null>(null);
  const canRevoke = hasGlobal(me.data, "pat:revoke-any");
  // Every PAT is visible to the Global admin only (pat:read-all): nothing is requested otherwise.
  const denied = me.data !== undefined && !hasGlobal(me.data, "pat:read-all");
  const list = useCursorList<AdminPat>({
    queryKey: [...idKeys.allPats, { status }],
    enabled: me.data !== undefined && !denied,
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/identity/admin/pats", {
        params: {
          query: {
            limit: 100,
            ...(status ? { status } : {}),
            ...(cursor ? { cursor } : {}),
          },
        },
      });
      if (data === undefined) throw new Error("GET /admin/pats had no body");
      return data;
    },
  });
  const filter = useRowFilter(
    list.rows,
    (row) =>
      `${row.name} ${row.owner_principal_id} ${nameOf(row.owner_principal_id) ?? ""}`,
  );
  const columns = patColumns<AdminPat>(
    (row) => {
      if (canRevoke) setRevoking(row);
    },
    [
      {
        id: "owner",
        header: "Owner",
        sortValue: (row) =>
          nameOf(row.owner_principal_id) ?? row.owner_principal_id,
        cell: (row) => (
          <span className={styles.principal}>
            <span>{nameOf(row.owner_principal_id) ?? "Unknown owner"}</span>
            <span className={styles.ident}>{row.owner_principal_id}</span>
          </span>
        ),
      },
    ],
  ).filter((column) => canRevoke || column.id !== "actions");

  return (
    <div className={styles.stack}>
      <PageHeader
        title="All access tokens"
        breadcrumbs={[{ label: "Administration" }]}
        description="Every personal access token on the platform. Secrets are never shown here."
      />
      <div className={styles.toolbar}>
        <div className={styles.toolbarField}>
          <SelectField
            id="pats-status"
            label="Status"
            controlSize="small"
            value={status}
            options={STATUS_OPTIONS}
            onChange={(event) => {
              setStatus(event.target.value as typeof status);
            }}
          />
        </div>
      </div>
      {denied ? (
        <NotAvailable what="The list of every access token" />
      ) : list.error ? (
        <ProblemAlert problem={problemOf(list.error)} onRetry={list.refetch} />
      ) : list.isPending ? (
        <Skeleton label="access tokens" rows={3} />
      ) : list.rows.length === 0 ? (
        <EmptyState
          icon={KeyRound}
          heading="No access tokens"
          body="Tokens people create from their account settings appear here."
        />
      ) : (
        <>
          <RowFilter id="all-pats" filter={filter} withInactive={false} />
          <DataTable
            caption="All access tokens"
            captionHidden
            columns={columns}
            rows={filter.filtered}
            getRowId={(row) => row.id}
            loadMore={{
              hasMore: list.hasMore,
              isLoadingMore: list.isLoadingMore,
              onLoadMore: list.loadMore,
              error: list.pageError,
            }}
          />
        </>
      )}
      {revoking ? (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setRevoking(null);
          }}
          title={`Revoke ${revoking.name}?`}
          body={`The token belongs to ${nameOf(revoking.owner_principal_id) ?? revoking.owner_principal_id}. Anything using it is refused from now on.`}
          objectName={revoking.name}
          confirmLabel={`Revoke ${revoking.name}`}
          busyLabel="Revoking…"
          onConfirm={async () => {
            await api.DELETE("/api/v1/identity/admin/pats/{credential_id}", {
              params: { path: { credential_id: revoking.id } },
            });
            await settle(idKeys.allPats, idKeys.myPats);
          }}
        />
      ) : null}
    </div>
  );
}
