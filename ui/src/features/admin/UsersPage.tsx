// Users (/admin/users, quick 261119-l30): local and Microsoft Entra users in one table with a
// Source column, a bookmarkable All | Local | Entra filter (`?source=`), a search box (`?q=`) and one
// "New" menu: "Create local user" (username, display name, email, initial password) and "Register
// Entra user" (by object ID). The UI never generates or shows a password; the admin types it and
// the API applies the password rules. Both lists need the root-level user:read: without it nothing
// is requested. The Entra list and menu entry appear only when Entra sign-in is configured.
import { useMutation } from "@tanstack/react-query";
import { Link, useNavigate, useRouteContext } from "@tanstack/react-router";
import { Users } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import type { Problem } from "@/api/problem";
import type { EntraUser, LocalUser } from "@/api/types";
import { StatusBadge } from "@/components/Badge/status";
import { Button } from "@/components/Button";
import { orNotSet, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { nameRuleError, PasswordField, TextField } from "@/components/form";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { NotAvailable } from "@/components/states";
import { hasGlobal, useMe } from "@/features/access/useMe";
import { useCursorList } from "@/lib/useCursorList";
import { problemOf } from "@/lib/unexpected";
import { EntraRegisterDialog } from "./EntraRegisterDialog";
import {
  MergedListView,
  NewMenu,
  sourcesShown,
  useMergedCursorLists,
  type Source,
  type SourceSearch,
} from "./SourceList";
import { ActiveBadge, idKeys, useApi, useSettle } from "./shared";
import styles from "./admin.module.css";

type UserRow = {
  key: string;
  source: Source;
  id: string;
  name: string;
  login: string | null;
  active: boolean;
  /** What the search box matches: name, username or email, and the Entra object ID. */
  text: string;
};

function localRow(user: LocalUser): UserRow {
  return {
    key: `local:${user.id}`,
    source: "local",
    id: user.id,
    name: user.display_name,
    login: user.username,
    active: user.is_active,
    text: `${user.display_name} ${user.username} ${user.email ?? ""}`,
  };
}

function entraRow(user: EntraUser): UserRow {
  return {
    key: `entra:${user.id}`,
    source: "entra",
    id: user.id,
    name: user.display_name,
    login: user.preferred_username,
    active: user.is_active,
    text: `${user.display_name} ${user.preferred_username ?? ""} ${user.object_id}`,
  };
}

export function UsersPage({
  search,
  onSearchChange,
}: {
  search: SourceSearch;
  onSearchChange: (next: SourceSearch) => void;
}) {
  const api = useApi();
  const me = useMe();
  const { config } = useRouteContext({ from: "__root__" });
  const canCreate = hasGlobal(me.data, "user:create");
  const [dialog, setDialog] = useState<"local" | "entra" | null>(null);
  const [hideInactive, setHideInactive] = useState(false);
  // Listing users needs the root-level user:read: without it nothing is requested.
  const denied = me.data !== undefined && !hasGlobal(me.data, "user:read");
  const ready = me.data !== undefined && !denied;
  const entraAvailable = config.entraEnabled && ready;
  const shown = sourcesShown(search, entraAvailable);

  const localList = useCursorList<LocalUser>({
    queryKey: idKeys.users,
    enabled: ready && shown.local,
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/identity/users", {
        params: { query: { limit: 100, ...(cursor ? { cursor } : {}) } },
      });
      if (data === undefined) throw new Error("GET /users had no body");
      return data;
    },
  });
  const entraList = useCursorList<EntraUser>({
    queryKey: idKeys.entraUsers,
    enabled: entraAvailable && shown.entra,
    fetchPage: async (cursor) => {
      const { data } = await api.GET("/api/v1/identity/entra/users", {
        params: { query: { limit: 100, ...(cursor ? { cursor } : {}) } },
      });
      if (data === undefined) throw new Error("GET /entra/users had no body");
      return data;
    },
  });
  const merged = useMergedCursorLists<UserRow>(
    [
      {
        ...localList,
        rows: localList.rows.map(localRow),
        label: "Local users",
        enabled: ready && shown.local,
      },
      {
        ...entraList,
        rows: entraList.rows.map(entraRow),
        label: "Entra users",
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

  const columns: Column<UserRow>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      sortValue: (row) => row.name,
      cell: (row) =>
        row.source === "local" ? (
          <Link
            to="/admin/users/$userId"
            params={{ userId: row.id }}
            className={styles.rowLink}
          >
            {row.name}
          </Link>
        ) : (
          <Link
            to="/admin/entra/users/$entraUserId"
            params={{ entraUserId: row.id }}
            className={styles.rowLink}
          >
            {row.name}
          </Link>
        ),
    },
    {
      id: "login",
      header: "Username/email",
      sortValue: (row) => row.login ?? "",
      cell: (row) => orNotSet(row.login),
    },
    {
      id: "source",
      header: "Source",
      sortValue: (row) => row.source,
      cell: (row) => <StatusBadge status={`source-${row.source}`} />,
    },
    {
      id: "status",
      header: "Status",
      cell: (row) => <ActiveBadge active={row.active} />,
    },
  ];

  const menu = (
    <NewMenu
      items={[
        ...(canCreate
          ? [
              {
                label: "Create local user",
                onSelect: () => {
                  setDialog("local");
                },
              },
            ]
          : []),
        ...(canCreate && config.entraEnabled
          ? [
              {
                label: "Register Entra user",
                onSelect: () => {
                  setDialog("entra");
                },
              },
            ]
          : []),
      ]}
    />
  );
  return (
    <div className={styles.stack}>
      <PageHeader
        title="Users"
        breadcrumbs={[{ label: "Administration" }]}
        description={
          entraAvailable
            ? "Local and Microsoft Entra users. Grants are managed on each folder's Access tab."
            : "Local user accounts. Grants are managed on each folder's Access tab."
        }
        actions={menu}
      />
      {denied ? (
        <NotAvailable what="The list of users" />
      ) : (
        <MergedListView
          id="users"
          plural="users"
          caption="Users"
          loading={me.isPending}
          entraAvailable={entraAvailable}
          search={search}
          onSearchChange={onSearchChange}
          hideDeactivated={{ checked: hideInactive, onChange: setHideInactive }}
          merged={merged}
          rows={filtered}
          columns={columns}
          getRowId={(row) => row.key}
          empty={{
            icon: Users,
            heading: "No users yet",
            body: entraAvailable
              ? "Local users sign in with a username and password; Entra users appear after their first sign-in."
              : "Local users sign in with a username and password.",
          }}
        />
      )}
      <CreateUserDialog
        open={dialog === "local"}
        onOpenChange={(open) => {
          setDialog(open ? "local" : null);
        }}
      />
      <EntraRegisterDialog
        kind="user"
        open={dialog === "entra"}
        onOpenChange={(open) => {
          setDialog(open ? "entra" : null);
        }}
      />
    </div>
  );
}

type Fields = "username" | "displayName" | "email" | "password";

function CreateUserDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const api = useApi();
  const settle = useSettle();
  const navigate = useNavigate();
  const [values, setValues] = useState<Record<Fields, string>>({
    username: "",
    displayName: "",
    email: "",
    password: "",
  });
  const [errors, setErrors] = useState<Partial<Record<Fields, string>>>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const create = useMutation({
    mutationFn: async () => {
      const { data } = await api.POST("/api/v1/identity/users", {
        body: {
          username: values.username.trim(),
          display_name: values.displayName.trim(),
          password: values.password,
          email: values.email.trim() === "" ? null : values.email.trim(),
        },
      });
      if (data === undefined) throw new Error("POST /users had no body");
      return data;
    },
  });

  function reset() {
    setValues({ username: "", displayName: "", email: "", password: "" });
    setErrors({});
    setProblem(null);
    create.reset();
  }

  function set(field: Fields, value: string) {
    setValues((current) => ({ ...current, [field]: value }));
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const found: Partial<Record<Fields, string>> = {};
    if (values.username.trim() === "") found.username = "Enter a username.";
    if (values.displayName.trim() === "")
      found.displayName = "Enter a display name.";
    else {
      const refused = nameRuleError(values.displayName);
      if (refused !== undefined) found.displayName = refused;
    }
    if (values.password === "") found.password = "Enter an initial password.";
    setErrors(found);
    setProblem(null);
    if (Object.keys(found).length > 0) return;
    create.mutate(undefined, {
      onSuccess: (user) => {
        void settle(idKeys.users);
        reset();
        onOpenChange(false);
        void navigate({
          to: "/admin/users/$userId",
          params: { userId: user.id },
        });
      },
      onError: (error) => {
        const found = problemOf(error);
        const mapped = mapFieldErrors(found, {
          "/username": "username",
          "/display_name": "displayName",
          "/email": "email",
          "/password": "password",
        });
        setErrors(mapped.fieldErrors);
        if (found.kind === "problem" && found.status === 409) {
          setErrors({ username: "A user with this username already exists." });
          return;
        }
        if (
          found.kind === "problem" &&
          found.slug !== null &&
          found.slug.includes("password")
        ) {
          setErrors({ password: found.detail ?? found.title });
          return;
        }
        if (Object.keys(mapped.fieldErrors).length === 0) setProblem(found);
      },
    });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Create local user"
      description="A local account that signs in with a username and password."
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
            form="create-user-form"
            busy={create.isPending}
            busyLabel="Creating…"
          >
            Create local user
          </Button>
        </>
      }
    >
      <form
        id="create-user-form"
        className={styles.form}
        noValidate
        onSubmit={onSubmit}
      >
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <TextField
          id="user-username"
          label="Username"
          autoComplete="off"
          spellCheck={false}
          value={values.username}
          error={errors.username}
          maxLength={64}
          onChange={(event) => {
            set("username", event.target.value);
          }}
        />
        <TextField
          id="user-display-name"
          label="Display name"
          autoComplete="off"
          value={values.displayName}
          error={errors.displayName ?? nameRuleError(values.displayName)}
          maxLength={200}
          onChange={(event) => {
            set("displayName", event.target.value);
          }}
        />
        <TextField
          id="user-email"
          label="Email (optional)"
          type="email"
          autoComplete="off"
          value={values.email}
          error={errors.email}
          onChange={(event) => {
            set("email", event.target.value);
          }}
        />
        <PasswordField
          id="user-password"
          label="Initial password"
          autoComplete="new-password"
          helper="12 to 128 characters. Common and breached passwords are refused. Share it with the user over a secure channel."
          value={values.password}
          error={errors.password}
          onChange={(event) => {
            set("password", event.target.value);
          }}
        />
      </form>
    </Dialog>
  );
}
