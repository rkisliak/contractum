// The members of an Entra group, read LIVE from Microsoft Graph (quick 261119-wp6). Contractum never
// stores Entra membership, so GET /api/v1/identity/entra/groups/{registration_id}/members asks the
// directory on every load (cached 30 s on the server). The card shows name (a link to the Contractum
// user or service-principal page when a record exists and the caller may open it), email or app ID,
// type in plain words and status. A 503 (directory unreachable, throttled or Entra not configured) is
// a warning with "Retry loading", never a red alert; a 404 means Entra no longer has the group.
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { EntraGroupMember } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { DataTable, orNotSet, type Column } from "@/components/DataTable";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { problemOf } from "@/lib/unexpected";
import { idKeys, useApi } from "./shared";
import styles from "./admin.module.css";

const KIND_LABEL: Record<EntraGroupMember["kind"], string> = {
  user: "User",
  group: "Group",
  service_principal: "Service principal",
  other: "Other",
};

function statusOf(member: EntraGroupMember): string {
  if (member.account_enabled === null) return "Not applicable";
  return member.account_enabled ? "Enabled" : "Disabled";
}

function NameCell({ member }: { member: EntraGroupMember }) {
  if (member.registered && member.record_id) {
    return member.kind === "user" ? (
      <Link
        to="/admin/entra/users/$entraUserId"
        params={{ entraUserId: member.record_id }}
        className={styles.rowLink}
      >
        {member.display_name}
      </Link>
    ) : (
      <Link
        to="/admin/entra/service-principals/$registrationId"
        params={{ registrationId: member.record_id }}
        className={styles.rowLink}
      >
        {member.display_name}
      </Link>
    );
  }
  return <>{member.display_name}</>;
}

const columns: Column<EntraGroupMember>[] = [
  {
    id: "name",
    header: "Name",
    rowHeader: true,
    sortValue: (m) => m.display_name.toLowerCase(),
    cell: (m) => <NameCell member={m} />,
  },
  {
    id: "secondary",
    header: "Email or app ID",
    sortValue: (m) => (m.secondary ?? "").toLowerCase(),
    cell: (m) => <span className={styles.ident}>{orNotSet(m.secondary)}</span>,
  },
  {
    id: "type",
    header: "Type",
    sortValue: (m) => KIND_LABEL[m.kind],
    cell: (m) => KIND_LABEL[m.kind],
  },
  {
    id: "status",
    header: "Status",
    sortValue: (m) => statusOf(m),
    cell: (m) => statusOf(m),
  },
];

export function EntraGroupMembers({
  registrationId,
}: {
  registrationId: string;
}) {
  const api = useApi();
  const query = useQuery({
    queryKey: [...idKeys.entraGroups, registrationId, "members"],
    queryFn: async () => {
      const { data } = await api.GET(
        "/api/v1/identity/entra/groups/{registration_id}/members",
        { params: { path: { registration_id: registrationId } } },
      );
      if (data === undefined) throw new Error("Entra members had no body");
      return data;
    },
  });

  let body;
  if (query.isError) {
    const problem = problemOf(query.error);
    const status = problem.kind === "problem" ? problem.status : undefined;
    body =
      status === 503 ? (
        <Alert
          intent="warning"
          action={
            <Button
              variant="secondary"
              size="small"
              onClick={() => {
                void query.refetch();
              }}
            >
              Retry loading
            </Button>
          }
        >
          Microsoft Entra ID could not be reached, or Entra is not configured,
          so the members cannot be shown right now. The registration itself is
          unaffected.
        </Alert>
      ) : status === 404 ? (
        <Alert intent="warning">
          Microsoft Entra ID no longer has this group, so it has no members to
          show. You can delete the registration.
        </Alert>
      ) : (
        <ProblemAlert problem={problem} onRetry={() => void query.refetch()} />
      );
  } else if (query.data === undefined) {
    body = <Skeleton label="members" rows={3} />;
  } else if (query.data.items.length === 0) {
    body = (
      <EmptyState
        level={3}
        heading="This group has no members"
        body="Microsoft Entra ID reports no users, groups or service principals in it."
      />
    );
  } else {
    const { items, truncated, total } = query.data;
    body = (
      <>
        {truncated ? (
          <p className={styles.muted}>
            {total !== null
              ? `Showing the first ${String(items.length)} of ${String(total)} members.`
              : `Showing the first ${String(items.length)} members.`}
          </p>
        ) : null}
        <DataTable
          caption="Members of this Entra group"
          captionHidden
          columns={columns}
          rows={items}
          getRowId={(m) => m.object_id}
        />
      </>
    );
  }

  return (
    <Card title="Members">
      <div className={styles.stack}>
        <p className={styles.factNote}>
          Live from Microsoft Entra ID; nested members included.
        </p>
        {body}
      </div>
    </Card>
  );
}
