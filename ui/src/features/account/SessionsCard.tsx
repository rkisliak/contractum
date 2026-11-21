// The Sessions card (quick 261120-s0p, ASVS 7.4.5 and 7.5.2): the live sign-in sessions of a local
// account, one row per session with its device summary, when it began, when it was last active and
// when it ends, the current one badged. Self mode (/account/profile) lets an interactive sign-in
// end one session or every other one; admin mode (/admin/users/$userId) lets a Global admin end
// one or all of a user's sessions. Both only shape what the API allows: every write is re-checked
// there. Session ids are never shown. A Microsoft Entra account has no local sessions: Entra
// manages them. A sign-out takes effect at the next refresh, so the card says how long an
// already issued access token still works.
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { Session, SessionList } from "@/api/types";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useToast } from "@/components/Toast";
import { hasGlobal, useMe } from "@/features/access/useMe";
import {
  formatDateTime,
  idKeys,
  useApi,
  useSettle,
} from "@/features/admin/shared";
import { problemOf } from "@/lib/unexpected";
import { MonitorSmartphone } from "lucide-react";
import styles from "./SessionsCard.module.css";

export type SessionsCardProps =
  { mode: "self" } | { mode: "admin"; userId: string; username: string };

const DESCRIPTION =
  "Browsers and API clients signed in with this account's password.";

function columns(
  onSignOut: ((row: Session) => void) | undefined,
): Column<Session>[] {
  const base: Column<Session>[] = [
    {
      id: "device",
      header: "Device",
      rowHeader: true,
      cell: (row) => (
        <span className={styles.device}>
          <span>{row.client ?? "Unknown device"}</span>
          {row.current ? <Badge intent="brand">This session</Badge> : null}
        </span>
      ),
    },
    {
      id: "started",
      header: "Signed in",
      sortValue: (row) => row.started_at,
      cell: (row) => formatDateTime(row.started_at),
    },
    {
      id: "active",
      header: "Last active",
      sortValue: (row) => row.last_active_at,
      cell: (row) => formatDateTime(row.last_active_at),
    },
    {
      id: "ends",
      header: "Ends",
      sortValue: (row) => row.expires_at,
      cell: (row) => formatDateTime(row.expires_at),
    },
  ];
  if (onSignOut === undefined) return base;
  return [
    ...base,
    {
      id: "actions",
      header: "Actions",
      align: "end",
      // The session making the request is ended by signing out, not from this list.
      cell: (row) =>
        row.current ? null : (
          <Button
            variant="ghost"
            size="small"
            aria-label={`Sign out ${row.client ?? "unknown device"}`}
            onClick={() => {
              onSignOut(row);
            }}
          >
            Sign out
          </Button>
        ),
    },
  ];
}

function others(count: number): string {
  return `${String(count)} other session${count === 1 ? "" : "s"}`;
}

export function SessionsCard(props: SessionsCardProps) {
  const api = useApi();
  const me = useMe();
  const settle = useSettle();
  const toast = useToast();
  const admin = props.mode === "admin";
  const scope = props.mode === "admin" ? props.userId : "me";
  const [target, setTarget] = useState<Session | null>(null);
  const [bulk, setBulk] = useState(false);

  const managedByEntra = !admin && me.data?.idp === "entra";
  // Admin mode needs user:read; nothing is requested before /me answers or for an Entra account.
  const allowed = admin
    ? hasGlobal(me.data, "user:read")
    : me.data !== undefined && !managedByEntra;
  const canSignOut = admin
    ? hasGlobal(me.data, "user:deactivate")
    : me.data?.interactive === true;

  const query = useQuery({
    queryKey: idKeys.sessions(scope),
    enabled: allowed,
    queryFn: async (): Promise<SessionList> => {
      const { data } =
        props.mode === "admin"
          ? await api.GET("/api/v1/identity/users/{user_id}/sessions", {
              params: { path: { user_id: props.userId } },
            })
          : await api.GET("/api/v1/identity/me/sessions");
      if (data === undefined) throw new Error("GET sessions had no body");
      return data;
    },
  });

  const signOut = useMutation({
    mutationFn: async (sessionId: string) => {
      if (props.mode === "admin") {
        await api.DELETE(
          "/api/v1/identity/users/{user_id}/sessions/{session_id}",
          {
            params: { path: { user_id: props.userId, session_id: sessionId } },
          },
        );
      } else {
        await api.DELETE("/api/v1/identity/me/sessions/{session_id}", {
          params: { path: { session_id: sessionId } },
        });
      }
    },
  });
  const signOutMany = useMutation({
    mutationFn: async (): Promise<number> => {
      const { data } =
        props.mode === "admin"
          ? await api.POST(
              "/api/v1/identity/users/{user_id}/sessions/revoke-all",
              { params: { path: { user_id: props.userId } } },
            )
          : await api.POST("/api/v1/identity/me/sessions/revoke-others");
      return data?.revoked ?? 0;
    },
  });

  if (!admin && managedByEntra) {
    return (
      <Card title="Sessions">
        <p className={styles.note}>
          Microsoft Entra ID manages your sign-in sessions.
        </p>
      </Card>
    );
  }
  if (admin && me.data !== undefined && !allowed) return null;

  const data = query.data;
  const hasOthers = data?.items.some((row) => !row.current) ?? false;
  const showBulk = canSignOut && hasOthers;
  const lifetimeMinutes =
    data === undefined ? 15 : Math.round(data.access_token_lifetime_s / 60);

  return (
    <Card title="Sessions">
      <div className={styles.stack}>
        <p className={styles.note}>{DESCRIPTION}</p>
        {showBulk ? (
          <div className={styles.toolbar}>
            <Button
              variant="secondary"
              size="small"
              onClick={() => {
                setBulk(true);
              }}
            >
              {admin ? "Sign out all sessions" : "Sign out everywhere else"}
            </Button>
          </div>
        ) : null}
        {query.isError ? (
          <ProblemAlert
            problem={problemOf(query.error)}
            onRetry={() => void query.refetch()}
          />
        ) : data === undefined ? (
          <Skeleton label="sessions" rows={2} />
        ) : data.items.length === 0 ? (
          <EmptyState
            icon={MonitorSmartphone}
            level={3}
            heading="No active sessions"
            body="Sessions signed in with a password appear here."
          />
        ) : (
          <>
            <DataTable
              caption={admin ? "Active sessions" : "Your active sessions"}
              captionHidden
              columns={columns(canSignOut ? setTarget : undefined)}
              rows={data.items}
              getRowId={(row) => row.id}
            />
            <p className={styles.note}>
              {String(data.total)} of at most {String(data.max_sessions)}{" "}
              sessions. Signing in beyond that ends the oldest one.
            </p>
            {canSignOut ? (
              <p className={styles.note}>
                A signed-out session ends at its next refresh; it keeps the
                access it already has for up to {String(lifetimeMinutes)}{" "}
                minutes.
              </p>
            ) : null}
          </>
        )}
      </div>
      {target ? (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setTarget(null);
          }}
          title="Sign out this session?"
          body="This session ends at its next refresh."
          dismissLabel="Keep session"
          confirmLabel="Sign out session"
          busyLabel="Signing out…"
          onConfirm={async () => {
            await signOut.mutateAsync(target.id);
            toast({ title: "Session signed out.", intent: "success" });
            await settle(idKeys.sessions(scope));
          }}
        />
      ) : null}
      <ConfirmDialog
        open={bulk}
        onOpenChange={setBulk}
        title={
          props.mode === "admin"
            ? `Sign out all of ${props.username}'s sessions?`
            : "Sign out every other session?"
        }
        body={
          props.mode === "admin"
            ? "Every session of this user ends at its next refresh."
            : "Every session except this one ends at its next refresh."
        }
        dismissLabel="Keep sessions"
        confirmLabel={
          admin ? "Sign out all sessions" : "Sign out everywhere else"
        }
        busyLabel="Signing out…"
        onConfirm={async () => {
          const revoked = await signOutMany.mutateAsync();
          toast({
            title: admin
              ? `Signed out ${String(revoked)} session${revoked === 1 ? "" : "s"}.`
              : `Signed out ${others(revoked)}.`,
            intent: "success",
          });
          await settle(idKeys.sessions(scope));
        }}
      />
    </Card>
  );
}
