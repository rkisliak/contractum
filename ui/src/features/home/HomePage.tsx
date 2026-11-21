// Signed-in home (UI-SPEC 7.5): "Welcome, {name}", the account card, the access card (the
// caller's role assignments from /me) and quick links into the main areas the caller can use. The
// "Global permissions" stat counts only ROOT-granted permissions, never the baseline ones every
// signed-in principal holds.
import {
  ChevronRight,
  FileText,
  FolderTree,
  KeyRound,
  ListChecks,
  ScrollText,
  ShieldCheck,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Link } from "@tanstack/react-router";
import type { Me } from "@/api/types";
import { Badge } from "@/components/Badge";
import { StatusBadge } from "@/components/Badge/status";
import { Card } from "@/components/Card";
import { PageHeader } from "@/components/PageHeader";
import { Stat, StatGrid } from "@/components/Stat";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import {
  EffectiveAccess,
  type AccessScope,
} from "@/features/access/EffectiveAccess";
import {
  homeRoles,
  readableFolderPath,
} from "@/features/access/effectiveAccessModel";
import {
  grantedGlobalPermissions,
  holdsAny,
  useHeldAccess,
  type HeldAccessQuery,
} from "@/features/access/useHeldAccess";
import { useMe } from "@/features/access/useMe";
import { useFolderList } from "@/features/folders/api";
import { problemOf } from "@/lib/unexpected";
import { principalTypeLabel } from "@/features/admin/principal";
import styles from "./HomePage.module.css";

export function HomePage() {
  const me = useMe();
  const access = useHeldAccess();
  const held = access.data;

  if (me.isError) {
    return (
      <ProblemAlert
        problem={problemOf(me.error)}
        onRetry={() => {
          void me.refetch();
        }}
      />
    );
  }
  if (me.data === undefined) {
    return <Skeleton label="your account" rows={2} />;
  }
  const data = me.data;
  return (
    <>
      <PageHeader
        title={`Welcome, ${data.display_name}`}
        documentTitle="Home"
        description="Your account, your folder access and the main areas of the platform."
      />
      <StatGrid label="Account summary">
        <Stat
          icon={FolderTree}
          label="Folder roles"
          value={data.assignments.length}
        />
        <Stat icon={Users} label="Groups" value={data.groups.length} />
        <Stat
          icon={ShieldCheck}
          label="Global permissions"
          value={grantedGlobalPermissions(data).length}
        />
        <Stat
          icon={KeyRound}
          label="Sign-in method"
          value={data.idp === "entra" ? "Entra ID" : "Local"}
        />
      </StatGrid>
      <div className={styles.grid}>
        <Card header="band" title="Your account">
          <dl className={styles.facts}>
            <dt>Name</dt>
            <dd>{data.display_name}</dd>
            <dt>{data.idp === "entra" ? "Email" : "Username"}</dt>
            <dd>{data.preferred_username ?? data.username ?? "Not set"}</dd>
            <dt>Sign-in method</dt>
            <dd>
              <StatusBadge status={data.idp === "entra" ? "entra" : "local"} />
            </dd>
            <dt>Principal type</dt>
            <dd>
              <Badge intent="neutral">
                {principalTypeLabel(data.principal_type, data.principal_id)}
              </Badge>
              {data.read_only ? (
                <>
                  {" "}
                  <Badge intent="warning">Read only</Badge>
                </>
              ) : null}
            </dd>
            <dt>Groups</dt>
            <dd>
              {data.groups.length === 0 ? (
                "None"
              ) : (
                <span className={styles.badges}>
                  {data.groups.map((group) => (
                    <Badge key={group.id} intent="neutral">
                      {group.display_name}
                    </Badge>
                  ))}
                </span>
              )}
            </dd>
          </dl>
        </Card>
        <div className={styles.wide}>
          <Card header="band" title="Your access">
            {data.assignments.length === 0 &&
            grantedGlobalPermissions(data).length === 0 ? (
              <EmptyState
                level={3}
                heading="No folder access yet"
                body="You are signed in, but no folder roles are assigned to you. Ask a folder admin to grant you access."
              />
            ) : (
              <HomeAccess me={data} access={access} />
            )}
          </Card>
        </div>
      </div>
      <section aria-labelledby="quick-links" className={styles.quick}>
        <h2 id="quick-links" className={styles.sectionTitle}>
          Quick links
        </h2>
        <ul className={styles.links}>
          <QuickLink
            to="/folders"
            icon={FolderTree}
            label="Browse folders"
            hint="Organizations, projects and teams you can see."
          />
          {held && holdsAny(held, "contract:create") ? (
            <QuickLink
              to="/folders"
              icon={FileText}
              label="New contract"
              hint="Open a folder, then create a contract there."
            />
          ) : null}
          {held && holdsAny(held, "ruleset:read", "ruleset:read-published") ? (
            <QuickLink
              to="/rule-sets"
              icon={ListChecks}
              label="Rule sets"
              hint="The governance rules contracts must pass."
            />
          ) : null}
          {held && holdsAny(held, "audit:query", "audit:read") ? (
            <QuickLink
              to="/audit"
              icon={ScrollText}
              label="Audit log"
              hint="Every change and access, recorded."
            />
          ) : null}
        </ul>
      </section>
    </>
  );
}

/**
 * The home access summary: every role held anywhere (with where it was granted) and what the
 * caller can do in at least one folder, from the capabilities at each folder holding a grant
 * (shared cache keys with the folder pages) plus the global permissions.
 */
function HomeAccess({ me, access }: { me: Me; access: HeldAccessQuery }) {
  const folders = useFolderList();
  if (access.error) {
    return (
      <ProblemAlert
        problem={problemOf(access.error)}
        onRetry={access.refetch}
      />
    );
  }
  if (access.data === undefined) {
    return <Skeleton label="your access" rows={3} />;
  }
  const folderList = folders.data?.folders ?? [];
  const roles = homeRoles(me, folderList);
  // One scope per folder holding a grant: Root first, then by readable path. The capability answer
  // already includes inherited roles, so it is used as is, never unioned across folders.
  const slugPaths = new Map(
    me.assignments.map((a) => [a.folder_id, a.folder_path]),
  );
  const scopes: AccessScope[] = access.data.byFolder
    .map(({ folderId, permissions }) => ({
      id: folderId,
      label: readableFolderPath(
        folderId,
        folderList,
        slugPaths.get(folderId) ?? "",
      ),
      permissions,
    }))
    .sort((a, b) =>
      a.label === "Root"
        ? b.label === "Root"
          ? 0
          : -1
        : b.label === "Root"
          ? 1
          : a.label.localeCompare(b.label),
    );
  return (
    <EffectiveAccess
      roles={roles}
      permissions={[...access.data.permissions]}
      scopes={scopes}
      initialScopeId={roles[0]?.sources[0]?.folderId}
      where="in at least one of your folders"
      fullAccess="Full access to everything."
      emptyRoles="You hold no folder role."
    />
  );
}

function QuickLink({
  to,
  icon: Icon,
  label,
  hint,
}: {
  to: string;
  icon: LucideIcon;
  label: string;
  hint: string;
}) {
  return (
    <li>
      <Link to={to} className={styles.link}>
        <span className={styles.linkIcon} aria-hidden="true">
          <Icon size={20} strokeWidth={1.75} />
        </span>
        <span className={styles.linkText}>
          <span className={styles.linkLabel}>{label}</span>
          <span>{hint}</span>
        </span>
        <ChevronRight
          size={16}
          strokeWidth={2}
          aria-hidden="true"
          className={styles.linkChevron}
        />
      </Link>
    </li>
  );
}
