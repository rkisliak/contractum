// The folder Access tab (D-UI-12-06, D-UI-12-07): grants made at this folder (revoke on a row),
// "Grant access" (principal picker, role select fed only by `grantable_roles`), the block
// inheritance switch on Organization and Team folders, and the caller's effective access. Every
// write invalidates grants, effective access, capabilities and `/me`.
import { WhenBy } from "@/components/DataTable/WhenBy";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { KeyRound, ShieldOff } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import type {
  EffectiveAccess as EffectiveAccessOut,
  Folder,
  Grant,
  GrantCreate,
} from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { SelectField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { EffectiveAccess } from "@/features/access/EffectiveAccess";
import {
  folderInheritance,
  folderRoles,
} from "@/features/access/effectiveAccessModel";
import { roleLabel } from "@/features/access/roles";
import { useCapabilities } from "@/features/access/useCapabilities";
import { useMe } from "@/features/access/useMe";
import { folderKeys, useFolderList } from "@/features/folders/api";
import { folderName } from "@/features/folders/tree";
import { useCursorList } from "@/lib/useCursorList";
import { problemOf } from "@/lib/unexpected";
import { PrincipalPicker, type PickedPrincipal } from "./PrincipalPicker";
import { principalTypeLabel, roleDescription } from "./principal";
import { formatDateTime, idKeys, useApi, useSettle } from "./shared";
import { usePrincipalNames } from "./usePrincipalNames";
import styles from "./admin.module.css";
import { PrincipalName, usePrincipalName } from "@/components/PrincipalName";

const BLOCKABLE_KINDS = new Set(["organization", "team"]);

function useAccessSettle(folderId: string) {
  const settle = useSettle();
  return () =>
    settle(
      idKeys.grants(folderId),
      idKeys.effective(folderId),
      folderKeys.all,
      ["identity", "principal-names"],
    );
}

/**
 * A grantee's name: from the principal lists the caller can read, else from identity's names
 * endpoint (an Org admin cannot read the user lists but may see who holds a grant).
 */
function useGranteeName(
  known: string | undefined,
  id: string | null | undefined,
): string | undefined {
  const remote = usePrincipalName(known === undefined ? id : null);
  return known ?? remote;
}

function GranteeName({ known, id }: { known: string | undefined; id: string }) {
  return <>{useGranteeName(known, id) ?? "Unknown principal"}</>;
}

function RevokeGrantButton({
  row,
  known,
  onClick,
}: {
  row: Grant;
  known: string | undefined;
  onClick: () => void;
}) {
  const name = useGranteeName(known, row.principal_id) ?? row.principal_id;
  return (
    <Button
      variant="ghost"
      size="small"
      aria-label={`Revoke ${roleLabel(row.role)} from ${name}`}
      onClick={onClick}
    >
      Revoke
    </Button>
  );
}

export function FolderAccess({ folder }: { folder: Folder }) {
  const api = useApi();
  const caps = useCapabilities(folder.id);
  const nameOf = usePrincipalNames();
  const refresh = useAccessSettle(folder.id);
  const [granting, setGranting] = useState(false);
  const [revoking, setRevoking] = useState<Grant | null>(null);
  const revokingName = useGranteeName(
    revoking ? nameOf(revoking.principal_id) : undefined,
    revoking?.principal_id,
  );
  const canManage = caps.can("grant:manage");
  // The grant list needs grant:read here (the API refuses it otherwise): without it the section
  // is left out and only inheritance and the caller's own access are shown.
  const canReadGrants = caps.can("grant:read");
  const grants = useCursorList<Grant>({
    queryKey: idKeys.grants(folder.id),
    enabled: canReadGrants,
    fetchPage: async (cursor) => {
      const { data } = await api.GET(
        "/api/v1/identity/folders/{folder_id}/grants",
        {
          params: {
            path: { folder_id: folder.id },
            query: { limit: 100, ...(cursor ? { cursor } : {}) },
          },
        },
      );
      if (data === undefined) throw new Error("GET grants had no body");
      return data;
    },
  });
  const where = folderName(folder);

  const columns: Column<Grant>[] = [
    {
      id: "principal",
      header: "Principal",
      rowHeader: true,
      sortValue: (row) => nameOf(row.principal_id) ?? row.principal_id,
      cell: (row) => (
        <span className={styles.principal}>
          <span className={styles.inline}>
            <span>
              <GranteeName
                known={nameOf(row.principal_id)}
                id={row.principal_id}
              />
            </span>
            <Badge intent="neutral">
              {principalTypeLabel(row.principal_type, row.principal_id)}
            </Badge>
          </span>
          <span className={styles.ident}>{row.principal_id}</span>
        </span>
      ),
    },
    {
      id: "role",
      header: "Role",
      sortValue: (row) => roleLabel(row.role),
      cell: (row) => roleLabel(row.role),
    },
    {
      id: "granted",
      header: "Granted",
      sortValue: (row) => row.created_at,
      cell: (row) => (
        <WhenBy
          when={formatDateTime(row.created_at)}
          by={nameOf(row.created_by) ?? <PrincipalName id={row.created_by} />}
        />
      ),
    },
    ...(canManage
      ? [
          {
            id: "actions",
            header: "Actions",
            align: "end" as const,
            cell: (row: Grant) => (
              <RevokeGrantButton
                row={row}
                known={nameOf(row.principal_id)}
                onClick={() => {
                  setRevoking(row);
                }}
              />
            ),
          },
        ]
      : []),
  ];

  const grantButton = canManage ? (
    <Button
      variant="primary"
      size="small"
      icon={KeyRound}
      onClick={() => {
        setGranting(true);
      }}
    >
      Grant access
    </Button>
  ) : null;

  return (
    <div className={styles.stack}>
      {canReadGrants ? (
        <section className={styles.stack} aria-labelledby="grants-heading">
          <div className={styles.sectionRow}>
            <h2 id="grants-heading" className={styles.sectionHeading}>
              Grants at this folder
            </h2>
            {grantButton}
          </div>
          <p className={styles.muted}>
            Grants inherited from above are shown on the folders where they were
            made. A grant here applies here and below.
          </p>
          {grants.error ? (
            <ProblemAlert
              problem={problemOf(grants.error)}
              onRetry={grants.refetch}
            />
          ) : grants.isPending ? (
            <Skeleton label="grants" rows={3} />
          ) : grants.rows.length === 0 ? (
            <EmptyState
              level={3}
              heading="No grants at this folder"
              body="Access here comes only from grants made above."
            />
          ) : (
            <DataTable
              caption={`Grants at ${where}`}
              captionHidden
              columns={columns}
              rows={grants.rows}
              getRowId={(row) => row.id}
              loadMore={{
                hasMore: grants.hasMore,
                isLoadingMore: grants.isLoadingMore,
                onLoadMore: grants.loadMore,
                error: grants.pageError,
              }}
            />
          )}
        </section>
      ) : null}
      <BlockInheritance folder={folder} />
      <EffectiveAccessPanel folder={folder} />
      <GrantDialog
        open={granting}
        onOpenChange={setGranting}
        folder={folder}
        grantableRoles={caps.data?.grantable_roles ?? []}
      />
      {revoking ? (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setRevoking(null);
          }}
          title={`Revoke ${roleLabel(revoking.role)} from ${revokingName ?? revoking.principal_id} at ${where}?`}
          body="It applies here and below."
          dismissLabel="Keep grant"
          confirmLabel="Revoke grant"
          busyLabel="Revoking…"
          onConfirm={async () => {
            await api.DELETE("/api/v1/identity/grants/{grant_id}", {
              params: { path: { grant_id: revoking.id } },
            });
            await refresh();
          }}
        />
      ) : null}
    </div>
  );
}

export function GrantDialog({
  open,
  onOpenChange,
  folder,
  grantableRoles,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  folder: Folder;
  grantableRoles: string[];
}) {
  const api = useApi();
  const refresh = useAccessSettle(folder.id);
  const [picked, setPicked] = useState<PickedPrincipal | null>(null);
  const [role, setRole] = useState("");
  const [errors, setErrors] = useState<{ principal?: string; role?: string }>(
    {},
  );
  const [problem, setProblem] = useState<Problem | null>(null);
  const grant = useMutation({
    mutationFn: async (input: { principal: PickedPrincipal; role: string }) => {
      await api.POST("/api/v1/identity/folders/{folder_id}/grants", {
        params: { path: { folder_id: folder.id } },
        body: {
          principal_type: input.principal.type,
          ...(input.principal.username === undefined
            ? { principal_id: input.principal.id }
            : { username: input.principal.username }),
          role: input.role as GrantCreate["role"],
        },
      });
    },
  });
  const where = folder.path === "" ? folderName(folder) : folder.path;

  function reset() {
    setPicked(null);
    setRole("");
    setErrors({});
    setProblem(null);
    grant.reset();
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const found: { principal?: string; role?: string } = {};
    if (picked === null) found.principal = "Choose who gets access.";
    if (role === "") found.role = "Choose a role.";
    setErrors(found);
    setProblem(null);
    if (picked === null || role === "") return;
    grant.mutate(
      { principal: picked, role },
      {
        onSuccess: () => {
          void refresh();
          reset();
          onOpenChange(false);
        },
        onError: (failure) => {
          setProblem(problemOf(failure));
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      size={512}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Grant access"
      description={`The role applies at ${where} and every folder below it.`}
      footer={
        <>
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              reset();
              onOpenChange(false);
            }}
          >
            Go back
          </Button>
          <Button
            variant="primary"
            size="small"
            type="submit"
            form="grant-form"
            busy={grant.isPending}
            busyLabel="Granting…"
            disabled={grantableRoles.length === 0}
          >
            Grant access
          </Button>
        </>
      }
    >
      <form
        id="grant-form"
        className={styles.form}
        noValidate
        onSubmit={onSubmit}
      >
        {problem ? (
          problem.kind === "problem" && problem.detail ? (
            <Alert intent="danger" title="The grant was refused">
              {problem.detail}
              {problem.slug ? (
                <>
                  {" "}
                  <Badge intent="danger" mono>
                    {problem.slug}
                  </Badge>
                </>
              ) : null}
            </Alert>
          ) : (
            <ProblemAlert problem={problem} context="action" />
          )
        ) : null}
        <PrincipalPicker
          types={[
            "user",
            "local_group",
            "entra_group",
            "service_account",
            "service_principal",
          ]}
          folderId={folder.id}
          value={picked}
          onChange={setPicked}
          error={errors.principal}
          allowUsername
        />
        {grantableRoles.length === 0 ? (
          <Alert intent="info">You cannot grant any role at this folder.</Alert>
        ) : (
          <SelectField
            id="grant-role"
            label="Role"
            placeholder="Choose a role"
            value={role}
            error={errors.role}
            helper={
              role
                ? roleDescription(role)
                : "Only roles you may grant here are listed."
            }
            options={grantableRoles.map((r) => ({
              value: r,
              label: roleLabel(r),
            }))}
            onChange={(event) => {
              setRole(event.target.value);
            }}
          />
        )}
        {picked && role ? (
          <p className={styles.summary}>
            Grant <strong>{roleLabel(role)}</strong> to{" "}
            <strong>
              {picked.label} (
              {principalTypeLabel(
                picked.type,
                picked.username === undefined ? picked.id : "local:",
              ).toLowerCase()}
              )
            </strong>{" "}
            at <strong>{where}</strong>
          </p>
        ) : null}
      </form>
    </Dialog>
  );
}

function BlockInheritance({ folder }: { folder: Folder }) {
  const api = useApi();
  const navigate = useNavigate();
  const refresh = useAccessSettle(folder.id);
  // folder:set-block is evaluated at the parent folder; its capabilities are only asked for when
  // the caller can see the parent (the API hides an unreadable folder behind a 404).
  const parentVisible =
    useFolderList().data?.folders.some((f) => f.id === folder.parent_id) ??
    false;
  const parentCaps = useCapabilities(folder.parent_id, {
    enabled: parentVisible,
  });
  const [confirming, setConfirming] = useState(false);
  const [lostAccess, setLostAccess] = useState(false);
  if (!BLOCKABLE_KINDS.has(folder.kind)) return null;
  const blocked = folder.block_inheritance;
  const where = folderName(folder);
  const canSet = parentCaps.can("folder:set-block");

  return (
    <Card title="Inheritance">
      <div className={styles.stack}>
        {lostAccess ? (
          <Alert intent="success">
            Saved. You no longer have access to this folder.
          </Alert>
        ) : null}
        <p className={styles.inline}>
          {blocked ? (
            <Badge intent="warning" icon={ShieldOff}>
              Inheritance blocked
            </Badge>
          ) : (
            <Badge intent="neutral">Inherits from above</Badge>
          )}
        </p>
        <p className={styles.muted}>
          {blocked
            ? "Grants made above this folder do not apply here or below. Global admins are exempt."
            : "Grants made above this folder apply here and below."}
        </p>
        {canSet ? (
          <div>
            <Button
              variant={blocked ? "secondary" : "outline-danger"}
              size="small"
              onClick={() => {
                setConfirming(true);
              }}
            >
              {blocked ? "Allow inheritance" : "Block inheritance"}
            </Button>
          </div>
        ) : null}
      </div>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={
          blocked
            ? `Allow inheritance at ${where}?`
            : `Block inheritance at ${where}?`
        }
        body={
          blocked
            ? "Grants made above start applying here and below again."
            : "Grants made above stop applying here and below. Global admins are exempt; mandatory rule sets and templates still flow down."
        }
        dismissLabel="Go back"
        confirmLabel={blocked ? "Allow inheritance" : "Block inheritance"}
        busyLabel="Saving…"
        onConfirm={async () => {
          const { data } = await api.PUT(
            "/api/v1/identity/folders/{folder_id}/block-inheritance",
            {
              params: { path: { folder_id: folder.id } },
              body: { enabled: !blocked },
            },
          );
          await refresh();
          if (data !== undefined && !("path" in data)) {
            setLostAccess(true);
            if (folder.parent_id)
              void navigate({
                to: "/folders/$folderId",
                params: { folderId: folder.parent_id },
              });
          }
        }}
      />
    </Card>
  );
}

function EffectiveAccessPanel({ folder }: { folder: Folder }) {
  const api = useApi();
  const me = useMe();
  const list = useFolderList(true);
  const access = useQuery({
    queryKey: idKeys.effective(folder.id),
    queryFn: async (): Promise<EffectiveAccessOut> => {
      const { data } = await api.GET(
        "/api/v1/identity/folders/{folder_id}/effective-access",
        { params: { path: { folder_id: folder.id } } },
      );
      if (data === undefined)
        throw new Error("GET effective-access had no body");
      return data;
    },
  });
  const folders = list.data?.folders ?? [folder];
  return (
    <Card title="Your effective access">
      {access.isError ? (
        <ProblemAlert
          problem={problemOf(access.error)}
          onRetry={() => void access.refetch()}
        />
      ) : access.data === undefined ? (
        <Skeleton label="effective access" rows={3} />
      ) : (
        <EffectiveAccess
          roles={folderRoles(me.data, folder, folders, access.data)}
          permissions={access.data.permissions}
          where="here and in the folders below"
          inheritance={folderInheritance(folder, folders, access.data)}
          emptyRoles="You hold no role at this folder or above it."
        />
      )}
    </Card>
  );
}
