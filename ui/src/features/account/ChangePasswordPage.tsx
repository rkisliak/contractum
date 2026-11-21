// /account/password (UI-SPEC 7.4): change the local password with the current one. Success revokes
// every session server-side, so the UI drops its token and sends the user to /signin with the
// password-changed notice. Entra and non-interactive principals get an info alert instead.
import { useMutation } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import { useRef, useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import { isApiError, type Problem } from "@/api/problem";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { PasswordField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { useMe } from "@/features/access/useMe";
import { problemOf } from "@/lib/unexpected";
import { AccountHeader, AccountPending } from "./AccountFrame";
import {
  PasswordRulesList,
  unmetRule,
  usePasswordRules,
} from "@/features/signin/passwordRules";
import styles from "./ChangePasswordPage.module.css";

type Errors = { current?: string; next?: string; confirm?: string };

export function ChangePasswordPage() {
  const { api, session } = useRouteContext({ from: "__root__" });
  const me = useMe();
  const rules = usePasswordRules();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const currentRef = useRef<HTMLInputElement>(null);
  const nextRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);

  const change = useMutation({
    mutationFn: async () => {
      await api.POST("/api/v1/identity/auth/password", {
        body: { current_password: current, new_password: next },
      });
    },
    onSuccess: () => {
      // The server revoked every session in the same transaction; this tab's token is dead, and a
      // query still in flight must not turn the notice into session-expired.
      session.end("password-changed");
    },
    onError: (error) => {
      if (isApiError(error) && error.problem.status === 401) {
        setErrors({ current: "Current password is incorrect." });
        setCurrent("");
        currentRef.current?.focus();
        return;
      }
      const found = problemOf(error);
      const mapped = mapFieldErrors(found, {
        "/current_password": "current",
        "/new_password": "next",
      });
      setErrors(mapped.fieldErrors);
      setProblem(
        mapped.unmapped.length > 0 ||
          Object.keys(mapped.fieldErrors).length === 0
          ? found
          : null,
      );
    },
  });

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    const found: Errors = {};
    if (current === "") found.current = "Enter your current password.";
    const unmet = unmetRule(rules, next);
    if (unmet !== null) found.next = unmet;
    if (confirm !== next) found.confirm = "Passwords do not match.";
    setErrors(found);
    if (found.current) currentRef.current?.focus();
    else if (found.next) nextRef.current?.focus();
    else if (found.confirm) confirmRef.current?.focus();
    else change.mutate();
  }

  const header = <AccountHeader title="Change password" />;

  if (me.isError || me.data === undefined)
    return <AccountPending title="Change password" me={me} />;
  if (me.data.idp !== "local" || !me.data.interactive) {
    return (
      <>
        {header}
        <div className={styles.narrow}>
          <Alert intent="info">
            Your password is managed by Microsoft. Change it in your Microsoft
            account.
          </Alert>
        </div>
      </>
    );
  }

  return (
    <>
      {header}
      <Card>
        <form className={styles.form} noValidate onSubmit={onSubmit}>
          {problem ? (
            <ProblemAlert
              problem={problem}
              context="action"
              fieldLabels={{
                "/current_password": "Current password",
                "/new_password": "New password",
              }}
            />
          ) : null}
          <PasswordField
            id="current-password"
            label="Current password"
            autoComplete="current-password"
            ref={currentRef}
            value={current}
            error={errors.current}
            onChange={(event) => {
              setCurrent(event.target.value);
            }}
          />
          <PasswordField
            id="new-password"
            label="New password"
            autoComplete="new-password"
            helper="Spaces and any characters are allowed, and a long passphrase works well."
            ref={nextRef}
            value={next}
            error={errors.next}
            onChange={(event) => {
              setNext(event.target.value);
            }}
          />
          <PasswordRulesList rules={rules} />
          <PasswordField
            id="confirm-password"
            label="Confirm new password"
            autoComplete="new-password"
            ref={confirmRef}
            value={confirm}
            error={errors.confirm}
            onChange={(event) => {
              setConfirm(event.target.value);
            }}
          />
          <Alert intent="info">
            You will be signed out on every device after changing your password.
          </Alert>
          <div>
            <Button
              type="submit"
              variant="primary"
              size="small"
              busy={change.isPending}
              busyLabel="Changing…"
            >
              Change password
            </Button>
          </div>
        </form>
      </Card>
    </>
  );
}
