// A local group (/admin/groups/$groupId, D-UI-12-05): details, rename (PATCH), delete (type the
// name), and members (list, add through the principal picker, remove). Joining a group counts as
// granting, so refusals (self-grant, ceiling) show the API detail after "Cannot add this member:".
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { UserPlus } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import type { GroupMember, LocalGroup } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { StatusBadge } from "@/components/Badge/status";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Identifier } from "@/components/Identifier";
import { DataTable, orNotSet, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { nameRuleError, TextField } from "@/components/form";
import { NotFound } from "@/components/NotFound";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useCursorList } from "@/lib/useCursorList";
import { isNotFound, problemOf } from "@/lib/unexpected";
import { PrincipalPicker, type PickedPrincipal } from "./PrincipalPicker";
import { principalTypeLabel } from "./principal";
import {
  formatDateTime,
  idKeys,
  useApi,
  useFolderLookup,
  useSettle,
} from "./shared";
import styles from "./admin.module.css";
import { PrincipalName } from "@/components/PrincipalName";

export function GroupDetailPage({ groupId }: { groupId: string }) {
  const api = useApi();
  const settle = useSettle();
  const navigate = useNavigate();
  const lookup = useFolderLookup();
  const [dialog, setDialog] = useState<
    "rename" | "delete" | "add" | { remove: GroupMember } | null
  >(null);
  const group = useQuery({
    queryKey: idKeys.group(groupId),
    queryFn: async (): Promise<LocalGroup> => {
      const { data } = await api.GET("/api/v1/identity/groups/{group_id}", {
        params: { path: { group_id: groupId } },
      });
      if (data === undefined) throw new Error("GET /groups/{id} had no body");
      return data;
    },
  });
  const members = useCursorList<GroupMember>({
    queryKey: idKeys.members(groupId),
    enabled: group.data !== undefined,
    fetchPage: async (cursor) => {
      const { data } = await api.GET(
        "/api/v1/identity/groups/{group_id}/members",
        {
          params: {
            path: { group_id: groupId },
            query: { limit: 100, ...(cursor ? { cursor } : {}) },
          },
        },
      );
      if (data === undefined) throw new Error("GET members had no body");
      return data;
    },
  });

  if (group.isError) {
    return isNotFound(group.error) ? (
      <NotFound back={{ label: "groups", to: "/admin/groups" }} />
    ) : (
      <ProblemAlert
        problem={problemOf(group.error)}
        onRetry={() => void group.refetch()}
      />
    );
  }
  if (group.data === undefined) return <Skeleton label="group" rows={4} />;
  const g = group.data;
  const close = (open: boolean) => {
    if (!open) setDialog(null);
  };

  const columns: Column<GroupMember>[] = [
    {
      id: "name",
      header: "Member",
      rowHeader: true,
      sortValue: (row) => row.display_name ?? row.member_id,
      cell: (row) => (
        <span className={styles.principal}>
          <span>{row.display_name ?? row.username ?? row.member_id}</span>
          <Identifier value={row.member_id} max="16rem" />
        </span>
      ),
    },
    {
      id: "type",
      header: "Type",
      cell: (row) => (
        <Badge intent="neutral">
          {principalTypeLabel(row.member_type, row.member_id)}
        </Badge>
      ),
    },
    {
      id: "username",
      header: "Username",
      cell: (row) => orNotSet(row.username),
    },
    {
      id: "added",
      header: "Added",
      sortValue: (row) => row.added_at,
      cell: (row) => formatDateTime(row.added_at),
    },
    {
      id: "actions",
      header: "Actions",
      align: "end",
      cell: (row) => (
        <Button
          variant="ghost"
          size="small"
          onClick={() => {
            setDialog({ remove: row });
          }}
        >
          Remove
        </Button>
      ),
    },
  ];

  const addButton = (
    <Button
      variant="primary"
      size="small"
      icon={UserPlus}
      onClick={() => {
        setDialog("add");
      }}
    >
      Add member
    </Button>
  );

  return (
    <div className={styles.stack}>
      <PageHeader
        title={g.name}
        breadcrumbs={[
          { label: "Administration" },
          { label: "Groups", to: "/admin/groups" },
          { label: g.name },
        ]}
        badges={<StatusBadge status="source-local" />}
        actions={
          <>
            <Button
              variant="secondary"
              size="small"
              onClick={() => {
                setDialog("rename");
              }}
            >
              Rename group
            </Button>
            <Button
              variant="outline-danger"
              size="small"
              onClick={() => {
                setDialog("delete");
              }}
            >
              Delete group
            </Button>
          </>
        }
      />
      <Card title="Details">
        <dl className={styles.facts}>
          <dt>Owner</dt>
          <dd>{lookup.ownerLabel(g.owner_folder_id)}</dd>
          <dt>Group id</dt>
          <dd className={styles.ident}>{g.id}</dd>
          <dt>Created</dt>
          <dd>
            {formatDateTime(g.created_at)} by{" "}
            <PrincipalName id={g.created_by} />
          </dd>
          <dt>Updated</dt>
          <dd>{formatDateTime(g.updated_at)}</dd>
        </dl>
      </Card>
      <section className={styles.stack} aria-labelledby="members-heading">
        <div className={styles.sectionRow}>
          <h2 id="members-heading" className={styles.sectionHeading}>
            Members
          </h2>
          {members.rows.length > 0 ? addButton : null}
        </div>
        {members.error ? (
          <ProblemAlert
            problem={problemOf(members.error)}
            onRetry={members.refetch}
          />
        ) : members.isPending ? (
          <Skeleton label="members" rows={3} />
        ) : members.rows.length === 0 ? (
          <EmptyState
            level={3}
            heading="No members yet"
            body="Add users or service accounts; they get every role granted to this group."
            action={addButton}
          />
        ) : (
          <DataTable
            caption={`Members of ${g.name}`}
            captionHidden
            columns={columns}
            rows={members.rows}
            getRowId={(row) => `${row.member_type}:${row.member_id}`}
            loadMore={{
              hasMore: members.hasMore,
              isLoadingMore: members.isLoadingMore,
              onLoadMore: members.loadMore,
              error: members.pageError,
            }}
          />
        )}
      </section>
      <RenameGroupDialog
        open={dialog === "rename"}
        onOpenChange={close}
        group={g}
      />
      <AddMemberDialog open={dialog === "add"} onOpenChange={close} group={g} />
      <ConfirmDialog
        open={dialog === "delete"}
        onOpenChange={close}
        title={`Delete ${g.name}?`}
        body="Grants to this group are removed, and its members lose the access those grants gave them."
        objectName={g.name}
        confirmLabel={`Delete ${g.name}`}
        typeToConfirm={g.name}
        busyLabel="Deleting…"
        onConfirm={async () => {
          await api.DELETE("/api/v1/identity/groups/{group_id}", {
            params: { path: { group_id: groupId } },
          });
          await settle(idKeys.groups, ["identity", "grants"]);
          void navigate({ to: "/admin/groups" });
        }}
      />
      {typeof dialog === "object" && dialog !== null ? (
        <ConfirmDialog
          open
          onOpenChange={close}
          title={`Remove ${dialog.remove.display_name ?? dialog.remove.member_id} from ${g.name}?`}
          body="They lose every role this group grants, here and below."
          dismissLabel="Keep member"
          confirmLabel="Remove member"
          busyLabel="Removing…"
          onConfirm={async () => {
            await api.DELETE(
              "/api/v1/identity/groups/{group_id}/members/{member_id}",
              {
                params: {
                  path: {
                    group_id: groupId,
                    member_id: dialog.remove.member_id,
                  },
                },
              },
            );
            await settle(idKeys.members(groupId));
          }}
        />
      ) : null}
    </div>
  );
}

function RenameGroupDialog({
  open,
  onOpenChange,
  group,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  group: LocalGroup;
}) {
  const api = useApi();
  const settle = useSettle();
  const [name, setName] = useState(group.name);
  const [error, setError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);
  // Reset whenever the dialog opens, however it was opened: the controlled Dialog never calls
  // onOpenChange(true), so a reset there would leave the last edit in the field.
  const [openedAs, setOpenedAs] = useState(open);
  if (open !== openedAs) {
    setOpenedAs(open);
    if (open) {
      setName(group.name);
      setError(undefined);
      setProblem(null);
    }
  }
  const rename = useMutation({
    mutationFn: async (next: string) => {
      await api.PATCH("/api/v1/identity/groups/{group_id}", {
        params: { path: { group_id: group.id } },
        body: { name: next },
      });
    },
  });

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    if (name.trim() === "") {
      setError("Enter a group name.");
      return;
    }
    const refused = nameRuleError(name);
    setError(refused);
    if (refused !== undefined) return;
    rename.mutate(name.trim(), {
      onSuccess: () => {
        void settle(idKeys.groups);
        onOpenChange(false);
      },
      onError: (failure) => {
        const found = problemOf(failure);
        if (found.kind === "problem" && found.status === 409) {
          setError("A group with this name already exists here.");
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
      title={`Rename ${group.name}`}
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
            form="rename-group-form"
            busy={rename.isPending}
            busyLabel="Renaming…"
          >
            Rename group
          </Button>
        </>
      }
    >
      <form
        id="rename-group-form"
        className={styles.form}
        noValidate
        onSubmit={onSubmit}
      >
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <TextField
          id="rename-group-name"
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

function AddMemberDialog({
  open,
  onOpenChange,
  group,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  group: LocalGroup;
}) {
  const api = useApi();
  const settle = useSettle();
  const [picked, setPicked] = useState<PickedPrincipal | null>(null);
  const [pickError, setPickError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);
  const add = useMutation({
    mutationFn: async (member: PickedPrincipal) => {
      await api.POST("/api/v1/identity/groups/{group_id}/members", {
        params: { path: { group_id: group.id } },
        body:
          member.username === undefined
            ? { principal_id: member.id }
            : { username: member.username },
      });
    },
  });

  function reset() {
    setPicked(null);
    setPickError(undefined);
    setProblem(null);
    add.reset();
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    if (picked === null) {
      setPickError("Choose a member to add.");
      return;
    }
    setPickError(undefined);
    add.mutate(picked, {
      onSuccess: () => {
        void settle(idKeys.members(group.id));
        reset();
        onOpenChange(false);
      },
      onError: (failure) => {
        setProblem(problemOf(failure));
      },
    });
  }

  return (
    <Dialog
      open={open}
      size={512}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title={`Add a member to ${group.name}`}
      description="Joining a group counts as granting its roles: you can only add members up to your own access."
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
            form="add-member-form"
            busy={add.isPending}
            busyLabel="Adding…"
          >
            Add member
          </Button>
        </>
      }
    >
      <form
        id="add-member-form"
        className={styles.form}
        noValidate
        onSubmit={onSubmit}
      >
        {problem ? (
          problem.kind === "problem" &&
          (problem.status === 403 || problem.status === 422) &&
          problem.detail ? (
            <Alert intent="danger" title="Cannot add this member:">
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
          types={["user", "service_account"]}
          value={picked}
          onChange={setPicked}
          error={pickError}
          allowUsername
        />
      </form>
    </Dialog>
  );
}
