// A local user (/admin/users/$userId, D-UI-12-04): profile, status and Deactivate (type the
// username) or Reactivate (plain confirmation). "Edit name" (quick 261119-16f) opens a dialog
// that changes the display name; it shows only to holders of user:update (a Global admin). There
// is no per-user grant listing API, so the page points at each folder's Access tab.
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import type { Problem } from "@/api/problem";
import type { LocalUser } from "@/api/types";
import { StatusBadge } from "@/components/Badge/status";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { orNotSet } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { nameRuleError, TextField } from "@/components/form";
import { NotFound } from "@/components/NotFound";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { useToast } from "@/components/Toast";
import { checkDisplayName } from "@/features/account/ProfilePage";
import { SessionsCard } from "@/features/account/SessionsCard";
import { hasGlobal, useMe } from "@/features/access/useMe";
import { isNotFound, problemOf } from "@/lib/unexpected";
import {
  ActiveBadge,
  formatDateTime,
  idKeys,
  useApi,
  useSettle,
} from "./shared";
import styles from "./admin.module.css";
import { PrincipalName } from "@/components/PrincipalName";

export function UserDetailPage({ userId }: { userId: string }) {
  const api = useApi();
  const me = useMe();
  const settle = useSettle();
  const [dialog, setDialog] = useState<
    "deactivate" | "reactivate" | "edit-name" | null
  >(null);
  const user = useQuery({
    queryKey: idKeys.user(userId),
    queryFn: async (): Promise<LocalUser> => {
      const { data } = await api.GET("/api/v1/identity/users/{user_id}", {
        params: { path: { user_id: userId } },
      });
      if (data === undefined) throw new Error("GET /users/{id} had no body");
      return data;
    },
  });

  if (user.isError) {
    return isNotFound(user.error) ? (
      <NotFound back={{ label: "users", to: "/admin/users" }} />
    ) : (
      <ProblemAlert
        problem={problemOf(user.error)}
        onRetry={() => void user.refetch()}
      />
    );
  }
  if (user.data === undefined) return <Skeleton label="user" rows={4} />;
  const u = user.data;
  const canDeactivate = hasGlobal(me.data, "user:deactivate");
  const canEdit = hasGlobal(me.data, "user:update");

  async function run(action: "deactivate" | "reactivate") {
    const path = `/api/v1/identity/users/{user_id}/${action}` as const;
    await api.POST(path, { params: { path: { user_id: userId } } });
    await settle(idKeys.users);
  }

  return (
    <div className={styles.stack}>
      <PageHeader
        title={u.display_name}
        breadcrumbs={[
          { label: "Administration" },
          { label: "Users", to: "/admin/users" },
          { label: u.display_name },
        ]}
        badges={
          <>
            <StatusBadge status="source-local" />
            <ActiveBadge active={u.is_active} />
          </>
        }
        actions={
          canEdit || canDeactivate ? (
            <>
              {canEdit ? (
                <Button
                  variant="secondary"
                  size="small"
                  onClick={() => {
                    setDialog("edit-name");
                  }}
                >
                  Edit name
                </Button>
              ) : null}
              {canDeactivate ? (
                u.is_active ? (
                  <Button
                    variant="danger"
                    size="small"
                    onClick={() => {
                      setDialog("deactivate");
                    }}
                  >
                    Deactivate user
                  </Button>
                ) : (
                  <Button
                    variant="secondary"
                    size="small"
                    onClick={() => {
                      setDialog("reactivate");
                    }}
                  >
                    Reactivate user
                  </Button>
                )
              ) : null}
            </>
          ) : null
        }
      />
      <div className={styles.cards}>
        <Card title="Profile">
          <dl className={styles.facts}>
            <dt>Username</dt>
            <dd>{u.username}</dd>
            <dt>Display name</dt>
            <dd>{u.display_name}</dd>
            <dt>Email</dt>
            <dd>{orNotSet(u.email)}</dd>
            <dt>Principal id</dt>
            <dd className={styles.ident}>{u.principal_id}</dd>
            <dt>Password change</dt>
            <dd>
              {u.must_rotate ? "Required at next sign-in" : "Not required"}
            </dd>
          </dl>
        </Card>
        <Card title="Record">
          <dl className={styles.facts}>
            <dt>Created</dt>
            <dd>{formatDateTime(u.created_at)}</dd>
            <dt>Created by</dt>
            <dd>
              <PrincipalName id={u.created_by} />
            </dd>
            <dt>Updated</dt>
            <dd>{formatDateTime(u.updated_at)}</dd>
            <dt>Grants</dt>
            <dd>Grants are managed on each folder&apos;s Access tab.</dd>
          </dl>
        </Card>
      </div>
      <SessionsCard mode="admin" userId={u.id} username={u.username} />
      <EditDisplayNameDialog
        open={dialog === "edit-name"}
        onOpenChange={(open) => {
          if (!open) setDialog(null);
        }}
        user={u}
      />
      <ConfirmDialog
        open={dialog === "deactivate"}
        onOpenChange={(open) => {
          if (!open) setDialog(null);
        }}
        title={`Deactivate ${u.username}?`}
        body="All their sessions, refresh tokens and PATs are revoked now. Reactivation restores grants but not tokens."
        objectName={u.username}
        confirmLabel={`Deactivate ${u.username}`}
        typeToConfirm={u.username}
        busyLabel="Deactivating…"
        onConfirm={() => run("deactivate")}
      />
      <ConfirmDialog
        open={dialog === "reactivate"}
        onOpenChange={(open) => {
          if (!open) setDialog(null);
        }}
        title={`Reactivate ${u.username}?`}
        body="They can sign in again and their grants apply again. Revoked tokens stay revoked."
        objectName={u.username}
        confirmLabel={`Reactivate ${u.username}`}
        busyLabel="Reactivating…"
        onConfirm={() => run("reactivate")}
      />
    </div>
  );
}

function EditDisplayNameDialog({
  open,
  onOpenChange,
  user,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  user: LocalUser;
}) {
  const api = useApi();
  const settle = useSettle();
  const toast = useToast();
  const [name, setName] = useState(user.display_name);
  const [error, setError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);
  // Reset whenever the dialog opens, however it was opened: the controlled Dialog never calls
  // onOpenChange(true), so a reset there would leave the last edit in the field.
  const [openedAs, setOpenedAs] = useState(open);
  if (open !== openedAs) {
    setOpenedAs(open);
    if (open) {
      setName(user.display_name);
      setError(undefined);
      setProblem(null);
    }
  }
  const rename = useMutation({
    mutationFn: async (next: string) => {
      await api.PATCH("/api/v1/identity/users/{user_id}", {
        params: { path: { user_id: user.id } },
        body: { display_name: next },
      });
    },
  });

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    const found = checkDisplayName(name);
    setError(found);
    if (found !== undefined) return;
    rename.mutate(name.trim(), {
      onSuccess: () => {
        void settle(idKeys.users, idKeys.principalName, idKeys.principalNames);
        toast({ title: "Display name saved.", intent: "success" });
        onOpenChange(false);
      },
      onError: (failure) => {
        const found = problemOf(failure);
        // Body validation answers /body/display_name; a domain refusal /display_name.
        const mapped = mapFieldErrors(found, {
          "/body/display_name": "displayName",
          "/display_name": "displayName",
        });
        setError(mapped.fieldErrors.displayName);
        setProblem(
          mapped.unmapped.length > 0 ||
            Object.keys(mapped.fieldErrors).length === 0
            ? found
            : null,
        );
      },
    });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Edit display name"
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
            form="edit-display-name-form"
            busy={rename.isPending}
            busyLabel="Saving…"
          >
            Save display name
          </Button>
        </>
      }
    >
      <form
        id="edit-display-name-form"
        className={styles.form}
        noValidate
        onSubmit={onSubmit}
      >
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <TextField
          id="edit-display-name"
          label="Display name"
          autoComplete="off"
          helper="Shown across the app and in the audit log."
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
