// /account/profile (quick 261119-16f): the signed-in person changes their own display name. A
// local account edits and saves; a Microsoft Entra account (or a non-interactive credential) sees
// the name read-only with the reason. After a save the shared invalidation refreshes /me (Home,
// Your account, the account menu) and every principal name ("by whom" cells). The API re-checks.
import { useMutation } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import type { Problem } from "@/api/problem";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { nameRuleError, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { useToast } from "@/components/Toast";
import { useMe } from "@/features/access/useMe";
import { idKeys, useApi, useSettle } from "@/features/admin/shared";
import { problemOf } from "@/lib/unexpected";
import { AccountHeader, AccountPending } from "./AccountFrame";
import { SessionsCard } from "./SessionsCard";
import styles from "./ProfilePage.module.css";

const MAX_LENGTH = 200;

export function checkDisplayName(value: string): string | undefined {
  const trimmed = value.trim();
  if (trimmed === "") return "Enter a display name.";
  if (trimmed.length > MAX_LENGTH) return "Use at most 200 characters.";
  return nameRuleError(value);
}

export function ProfilePage() {
  const api = useApi();
  const settle = useSettle();
  const toast = useToast();
  const me = useMe();
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);

  const save = useMutation({
    mutationFn: async (name: string) => {
      await api.PATCH("/api/v1/identity/me", {
        body: { display_name: name },
      });
    },
  });

  const header = <AccountHeader title="Profile" />;

  if (me.isError || me.data === undefined)
    return <AccountPending title="Profile" me={me} />;

  const current = me.data.display_name;
  const editable = me.data.idp === "local" && me.data.interactive;

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    const value = draft ?? current;
    const found = checkDisplayName(value);
    setError(found);
    if (found !== undefined) return;
    save.mutate(value.trim(), {
      onSuccess: () => {
        setDraft(null);
        toast({ title: "Display name saved.", intent: "success" });
        void settle(idKeys.users, idKeys.principalName, idKeys.principalNames);
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
    <>
      {header}
      <Card title="Profile">
        <form className={styles.form} noValidate onSubmit={onSubmit}>
          {problem ? (
            <ProblemAlert
              problem={problem}
              context="action"
              fieldLabels={{
                "/body/display_name": "Display name",
                "/display_name": "Display name",
              }}
            />
          ) : null}
          {editable ? (
            <TextField
              id="display-name"
              label="Display name"
              autoComplete="name"
              maxLength={MAX_LENGTH}
              helper="Shown across the app and in the audit log."
              value={draft ?? current}
              error={
                error ?? (draft === null ? undefined : nameRuleError(draft))
              }
              onChange={(event) => {
                setDraft(event.target.value);
              }}
            />
          ) : (
            <TextField
              id="display-name"
              label="Display name"
              autoComplete="name"
              readOnly
              helper={
                me.data.idp === "entra"
                  ? "Your name comes from Microsoft Entra ID"
                  : "Sign in interactively to change your display name."
              }
              value={current}
            />
          )}
          {editable ? (
            <div>
              <Button
                type="submit"
                variant="primary"
                size="small"
                busy={save.isPending}
                busyLabel="Saving…"
              >
                Save
              </Button>
            </div>
          ) : null}
        </form>
      </Card>
      <SessionsCard mode="self" />
    </>
  );
}
