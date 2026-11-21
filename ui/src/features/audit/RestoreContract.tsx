// "Restore contract" on a `contract.deleted` audit event (D-56): the only screen that reaches a
// soft-deleted contract after the post-delete notice is gone. Shown only when the capabilities at
// the contract's folder (from the event snapshot) include `contract:restore`; the API re-checks.
// A contract that was already restored answers 200 unchanged, so a second click is harmless.
import { useQueryClient } from "@tanstack/react-query";
import { Link, useRouteContext } from "@tanstack/react-router";
import { Undo2 } from "lucide-react";
import { useState } from "react";
import type { Problem } from "@/api/problem";
import type { AuditEvent } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { ProblemAlert } from "@/components/ProblemAlert";
import { useCapabilities } from "@/features/access/useCapabilities";
import { contractKeys } from "@/features/contracts/api";
import { problemOf } from "@/lib/unexpected";
import styles from "./Audit.module.css";

function snapshotText(event: AuditEvent, key: string): string | null {
  for (const snapshot of [event.before, event.after]) {
    if (snapshot && typeof snapshot === "object" && !Array.isArray(snapshot)) {
      const value = (snapshot as Record<string, unknown>)[key];
      if (typeof value === "string" && value !== "") return value;
    }
  }
  return null;
}

export function RestoreContract({ event }: { event: AuditEvent }) {
  const { api } = useRouteContext({ from: "__root__" });
  const queryClient = useQueryClient();
  const folderId = snapshotText(event, "folder_id");
  const name = snapshotText(event, "odcs_id") ?? "The contract";
  const caps = useCapabilities(folderId, { enabled: folderId !== null });
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [restored, setRestored] = useState(false);
  const contractId = event.aggregate_id;

  if (folderId === null || !caps.can("contract:restore")) return null;
  if (restored)
    return (
      <Alert intent="success" title={`${name} was restored`}>
        <Link
          to="/contracts/$contractId"
          params={{ contractId }}
          className={styles.link}
        >
          Open {name}
        </Link>
      </Alert>
    );
  return (
    <section className={styles.section} aria-labelledby="audit-restore">
      <h3 id="audit-restore" className={styles.sectionTitle}>
        Deleted contract
      </h3>
      <p>
        It no longer appears in lists or fetches. Restoring it brings back its
        drafts and versions as they were.
      </p>
      {problem ? <ProblemAlert problem={problem} context="action" /> : null}
      <div>
        <Button
          variant="secondary"
          size="small"
          icon={Undo2}
          busy={busy}
          busyLabel="Restoring…"
          onClick={() => {
            setBusy(true);
            setProblem(null);
            api
              .POST("/api/v1/contracts/contracts/{contract_id}/restore", {
                params: { path: { contract_id: contractId } },
              })
              .then(
                async () => {
                  setRestored(true);
                  await queryClient.invalidateQueries({
                    queryKey: contractKeys.lists,
                  });
                },
                (error: unknown) => {
                  setProblem(problemOf(error));
                },
              )
              .finally(() => {
                setBusy(false);
              });
          }}
        >
          Restore contract
        </Button>
      </div>
    </section>
  );
}
