// A service account (/admin/service-accounts/$serviceAccountId, D-UI-12-10): details, rename,
// deactivate (type the name; every key is revoked) or reactivate, and its keys: list with expiry
// warnings, "Create key" (show-once secret), revoke (type the key name). At most two keys are
// active; the rotation recipe shows when a key nears expiry.
import { useMutation, useQuery } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import type { ServiceAccount, ServiceAccountKey } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { StatusBadge } from "@/components/Badge/status";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { nameRuleError, TextField } from "@/components/form";
import { NotFound } from "@/components/NotFound";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { SecretRevealDialog } from "@/components/SecretRevealDialog";
import { EmptyState, Skeleton } from "@/components/states";
import { isNotFound, problemOf } from "@/lib/unexpected";
import { CreateCredentialDialog } from "./CreateCredentialDialog";
import {
  ActiveBadge,
  CredentialStatus,
  daysUntil,
  formatDateTime,
  idKeys,
  useApi,
  useFolderLookup,
  useSettle,
} from "./shared";
import styles from "./admin.module.css";
import { PrincipalName } from "@/components/PrincipalName";

export const KEY_LIMIT_COPY =
  "This account already has two active keys. Revoke one first (rotate: add key 2, switch your pipeline, revoke key 1).";

type DialogState =
  | "rename"
  | "deactivate"
  | "reactivate"
  | "create-key"
  | { revoke: ServiceAccountKey }
  | null;

export function ServiceAccountDetailPage({ accountId }: { accountId: string }) {
  const api = useApi();
  const settle = useSettle();
  const lookup = useFolderLookup();
  const [dialog, setDialog] = useState<DialogState>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const account = useQuery({
    queryKey: idKeys.serviceAccount(accountId),
    queryFn: async (): Promise<ServiceAccount> => {
      const { data } = await api.GET(
        "/api/v1/identity/service-accounts/{service_account_id}",
        { params: { path: { service_account_id: accountId } } },
      );
      if (data === undefined)
        throw new Error("GET service account had no body");
      return data;
    },
  });
  const keys = useQuery({
    queryKey: idKeys.keys(accountId),
    enabled: account.data !== undefined,
    queryFn: async (): Promise<ServiceAccountKey[]> => {
      const { data } = await api.GET(
        "/api/v1/identity/service-accounts/{service_account_id}/keys",
        { params: { path: { service_account_id: accountId } } },
      );
      if (data === undefined) throw new Error("GET keys had no body");
      return data.items;
    },
  });

  if (account.isError) {
    return isNotFound(account.error) ? (
      <NotFound
        back={{ label: "service accounts", to: "/admin/service-accounts" }}
      />
    ) : (
      <ProblemAlert
        problem={problemOf(account.error)}
        onRetry={() => void account.refetch()}
      />
    );
  }
  if (account.data === undefined)
    return <Skeleton label="service account" rows={4} />;
  const a = account.data;
  const close = (open: boolean) => {
    if (!open) setDialog(null);
  };
  const active = (keys.data ?? []).filter((k) => k.status === "active");
  const nearingExpiry = active.some((k) => k.expiring_soon);

  async function run(action: "deactivate" | "reactivate") {
    if (action === "deactivate")
      await api.POST(
        "/api/v1/identity/service-accounts/{service_account_id}/deactivate",
        { params: { path: { service_account_id: accountId } } },
      );
    else
      await api.POST(
        "/api/v1/identity/service-accounts/{service_account_id}/reactivate",
        { params: { path: { service_account_id: accountId } } },
      );
    await settle(idKeys.serviceAccounts);
  }

  const columns: Column<ServiceAccountKey>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      sortValue: (row) => row.name,
      cell: (row) => row.name,
    },
    {
      id: "status",
      header: "Status",
      cell: (row) => <CredentialStatus status={row.status} />,
    },
    {
      id: "expires",
      header: "Expires",
      sortValue: (row) => row.expires_at,
      cell: (row) => {
        const days = daysUntil(row.expires_at);
        return (
          <span className={styles.principal}>
            <span className={styles.oneLine}>
              {formatDateTime(row.expires_at)}
            </span>
            {row.status === "active" ? (
              days < 30 ? (
                <Badge intent="warning">
                  {`Expires in ${String(days)} day${days === 1 ? "" : "s"}`}
                </Badge>
              ) : (
                <span
                  className={styles.muted}
                >{`Expires in ${String(days)} days`}</span>
              )
            ) : null}
          </span>
        );
      },
    },
    {
      id: "created",
      header: "Created",
      sortValue: (row) => row.created_at,
      cell: (row) => formatDateTime(row.created_at),
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
            onClick={() => {
              setDialog({ revoke: row });
            }}
          >
            Revoke
          </Button>
        ) : null,
    },
  ];

  const createKeyButton = a.is_active ? (
    <Button
      variant="primary"
      size="small"
      icon={KeyRound}
      onClick={() => {
        setDialog("create-key");
      }}
    >
      Create key
    </Button>
  ) : null;

  return (
    <div className={styles.stack}>
      <PageHeader
        title={a.name}
        breadcrumbs={[
          { label: "Administration" },
          { label: "Service accounts", to: "/admin/service-accounts" },
          { label: a.name },
        ]}
        badges={
          <>
            <StatusBadge status="source-local" />
            <ActiveBadge active={a.is_active} />
          </>
        }
        actions={
          <>
            <Button
              variant="secondary"
              size="small"
              onClick={() => {
                setDialog("rename");
              }}
            >
              Rename service account
            </Button>
            {a.is_active ? (
              <Button
                variant="outline-danger"
                size="small"
                onClick={() => {
                  setDialog("deactivate");
                }}
              >
                Deactivate
              </Button>
            ) : (
              <Button
                variant="secondary"
                size="small"
                onClick={() => {
                  setDialog("reactivate");
                }}
              >
                Reactivate
              </Button>
            )}
          </>
        }
      />
      <Card title="Details">
        <dl className={styles.facts}>
          <dt>Owner</dt>
          <dd>{lookup.ownerLabel(a.owner_folder_id)}</dd>
          <dt>Principal id</dt>
          <dd className={styles.ident}>{a.principal_id}</dd>
          <dt>Created</dt>
          <dd>
            {formatDateTime(a.created_at)} by{" "}
            <PrincipalName id={a.created_by} />
          </dd>
          <dt>Grants</dt>
          <dd>Grant this account roles on each folder&apos;s Access tab.</dd>
        </dl>
      </Card>
      <section className={styles.stack} aria-labelledby="keys-heading">
        <div className={styles.sectionRow}>
          <h2 id="keys-heading" className={styles.sectionHeading}>
            API keys
          </h2>
          {(keys.data?.length ?? 0) > 0 ? createKeyButton : null}
        </div>
        {nearingExpiry ? (
          <Alert intent="warning" title="A key expires soon">
            Rotate without downtime: create a second key, switch your pipeline
            to it, then revoke the old key.
          </Alert>
        ) : null}
        {keys.isError ? (
          <ProblemAlert
            problem={problemOf(keys.error)}
            onRetry={() => void keys.refetch()}
          />
        ) : keys.data === undefined ? (
          <Skeleton label="keys" rows={2} />
        ) : keys.data.length === 0 ? (
          <EmptyState
            level={3}
            heading="No keys yet"
            body={
              a.is_active
                ? "Create a key and store it in your pipeline's secret manager."
                : "Reactivate this account to create keys."
            }
            action={createKeyButton}
          />
        ) : (
          <DataTable
            caption={`Keys of ${a.name}`}
            captionHidden
            columns={columns}
            rows={keys.data}
            getRowId={(row) => row.id}
          />
        )}
      </section>
      <RenameDialog
        open={dialog === "rename"}
        onOpenChange={close}
        account={a}
      />
      <CreateCredentialDialog
        open={dialog === "create-key"}
        onOpenChange={close}
        title="Create key"
        description={`A new API key for ${a.name}. It is shown once.`}
        submitLabel="Create key"
        defaultDays={90}
        refusalCopy={{ "credential-limit-reached": KEY_LIMIT_COPY }}
        create={async (input) => {
          const { data } = await api.POST(
            "/api/v1/identity/service-accounts/{service_account_id}/keys",
            {
              params: { path: { service_account_id: accountId } },
              body: { name: input.name, expires_at: input.expiresAt },
            },
          );
          if (data === undefined) throw new Error("POST key had no body");
          void settle(idKeys.keys(accountId));
          return data.token;
        }}
        onCreated={setSecret}
      />
      <SecretRevealDialog
        open={secret !== null}
        kind="key"
        secret={secret ?? ""}
        onClosed={() => {
          setSecret(null);
        }}
      />
      <ConfirmDialog
        open={dialog === "deactivate"}
        onOpenChange={close}
        title={`Deactivate ${a.name}?`}
        body="All its keys are revoked now. Reactivation restores grants but not keys."
        objectName={a.name}
        confirmLabel={`Deactivate ${a.name}`}
        typeToConfirm={a.name}
        busyLabel="Deactivating…"
        onConfirm={() => run("deactivate")}
      />
      <ConfirmDialog
        open={dialog === "reactivate"}
        onOpenChange={close}
        title={`Reactivate ${a.name}?`}
        body="Its grants apply again. Create a new key for its pipeline."
        objectName={a.name}
        confirmLabel={`Reactivate ${a.name}`}
        busyLabel="Reactivating…"
        onConfirm={() => run("reactivate")}
      />
      {typeof dialog === "object" && dialog !== null ? (
        <ConfirmDialog
          open
          onOpenChange={close}
          title={`Revoke ${dialog.revoke.name}?`}
          body="Pipelines using this key are refused from now on. This cannot be undone."
          objectName={dialog.revoke.name}
          confirmLabel={`Revoke ${dialog.revoke.name}`}
          typeToConfirm={dialog.revoke.name}
          busyLabel="Revoking…"
          onConfirm={async () => {
            await api.DELETE(
              "/api/v1/identity/service-accounts/{service_account_id}/keys/{key_id}",
              {
                params: {
                  path: {
                    service_account_id: accountId,
                    key_id: dialog.revoke.id,
                  },
                },
              },
            );
            await settle(idKeys.keys(accountId));
          }}
        />
      ) : null}
    </div>
  );
}

function RenameDialog({
  open,
  onOpenChange,
  account,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  account: ServiceAccount;
}) {
  const api = useApi();
  const settle = useSettle();
  const [name, setName] = useState(account.name);
  const [error, setError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);
  // Reset whenever the dialog opens, however it was opened: the controlled Dialog never calls
  // onOpenChange(true), so a reset there would leave the last edit in the field.
  const [openedAs, setOpenedAs] = useState(open);
  if (open !== openedAs) {
    setOpenedAs(open);
    if (open) {
      setName(account.name);
      setError(undefined);
      setProblem(null);
    }
  }
  const rename = useMutation({
    mutationFn: async (next: string) => {
      await api.PATCH(
        "/api/v1/identity/service-accounts/{service_account_id}",
        {
          params: { path: { service_account_id: account.id } },
          body: { name: next },
        },
      );
    },
  });

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    if (name.trim() === "") {
      setError("Enter a name.");
      return;
    }
    const refused = nameRuleError(name);
    setError(refused);
    if (refused !== undefined) return;
    rename.mutate(name.trim(), {
      onSuccess: () => {
        void settle(
          idKeys.serviceAccounts,
          idKeys.principalName,
          idKeys.principalNames,
        );
        onOpenChange(false);
      },
      onError: (failure) => {
        const found = problemOf(failure);
        if (found.kind === "problem" && found.status === 409) {
          setError("A service account with this name already exists here.");
          return;
        }
        setProblem(found);
      },
    });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Rename ${account.name}`}
      footer={
        <>
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              onOpenChange(false);
            }}
          >
            Go back
          </Button>
          <Button
            variant="primary"
            size="small"
            type="submit"
            form="rename-sa-form"
            busy={rename.isPending}
            busyLabel="Renaming…"
          >
            Rename service account
          </Button>
        </>
      }
    >
      <form
        id="rename-sa-form"
        className={styles.form}
        noValidate
        onSubmit={onSubmit}
      >
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <TextField
          id="rename-sa-name"
          label="Name"
          autoComplete="off"
          value={name}
          error={error ?? nameRuleError(name)}
          maxLength={200}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </form>
    </Dialog>
  );
}
