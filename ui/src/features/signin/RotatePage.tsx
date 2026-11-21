// /signin/rotate (UI-SPEC 7.3, D-UI-08-15): a blocking standalone card that replaces the
// one-time bootstrap password. The restricted token never leaves the session; on success the
// session signs in again on the cookie path and the user lands where they were heading.
import { Link, useRouter } from "@tanstack/react-router";
import { useEffect, useRef, useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import { isApiError, type Problem } from "@/api/problem";
import type { PasswordRules } from "@/api/types";
import { safeRedirect } from "@/auth/redirect";
import type { Session } from "@/auth/session";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { PasswordField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { useToast } from "@/components/Toast";
import { problemOf } from "@/lib/unexpected";
import { rotateCopy } from "./copy";
import {
  PasswordRulesList,
  unmetRule,
  usePasswordRules,
} from "./passwordRules";
import styles from "./RotatePage.module.css";

type Errors = { next?: string; confirm?: string };

export type RotatePageProps = {
  session: Session;
  /** The `redirect` search param, untrusted. */
  redirect?: string | undefined;
};

function validate(next: string, confirm: string, rules: PasswordRules): Errors {
  const found: Errors = {};
  const unmet = unmetRule(rules, next);
  if (unmet !== null) found.next = unmet;
  if (confirm !== next) found.confirm = rotateCopy.mismatch;
  return found;
}

export function RotatePage({ session, redirect }: RotatePageProps) {
  const router = useRouter();
  const toast = useToast();
  const rules = usePasswordRules();
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [submitted, setSubmitted] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const nextRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    document.title = rotateCopy.documentTitle;
  }, []);

  function backToSignIn(notice?: "rotation-restart") {
    session.clearRestrictedToken();
    void router.navigate({
      to: "/signin",
      search: {
        ...(redirect === undefined ? {} : { redirect }),
        ...(notice === undefined ? {} : { notice }),
      },
    });
  }

  async function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    setSubmitted(true);
    setProblem(null);
    const found = validate(next, confirm, rules);
    setErrors(found);
    if (found.next) {
      nextRef.current?.focus();
      return;
    }
    if (found.confirm) {
      confirmRef.current?.focus();
      return;
    }

    inFlight.current = true;
    setBusy(true);
    try {
      const result = await session.rotate(next);
      if (result.kind === "signed-in") {
        toast({ title: rotateCopy.success });
        router.history.push(safeRedirect(redirect));
        return;
      }
      // The new password is itself restricted (not expected): start again from sign-in.
      backToSignIn("rotation-restart");
      return;
    } catch (error) {
      const found = problemOf(error);
      if (isApiError(error) && error.problem.status === 401) {
        // The restricted token expired while the form was open.
        backToSignIn("rotation-restart");
        return;
      }
      const mapped = mapFieldErrors(found, { "/new_password": "next" });
      const fieldErrors = mapped.fieldErrors as Errors;
      setErrors(fieldErrors);
      // The alert lists only what no field shows, so a refusal is never repeated.
      setProblem(
        Object.keys(fieldErrors).length === 0
          ? found
          : mapped.unmapped.length > 0 && found.kind === "problem"
            ? { ...found, errors: mapped.unmapped }
            : null,
      );
      if (fieldErrors.next) nextRef.current?.focus();
    }
    inFlight.current = false;
    setBusy(false);
  }

  // After the first submit each field re-validates as it changes (UI-SPEC 5.2).
  function revalidate(nextValue: string, confirmValue: string) {
    if (submitted) setErrors(validate(nextValue, confirmValue, rules));
  }

  return (
    <main className={styles.page}>
      <div className={styles.column}>
        <Link to="/signin" className={styles.mark}>
          {rotateCopy.brandMark}
        </Link>
        <Card title={rotateCopy.heading} titleAs="h1" titleSize="page">
          <p className={styles.lead}>{rotateCopy.lead}</p>
          <form
            className={styles.form}
            noValidate
            onSubmit={(event) => {
              void handleSubmit(event);
            }}
          >
            {problem ? (
              <ProblemAlert
                problem={problem}
                context="signin"
                fieldLabels={{ "/new_password": rotateCopy.newPassword }}
              />
            ) : null}
            <div className={styles.fields}>
              <PasswordField
                ref={nextRef}
                id="rotate-new-password"
                label={rotateCopy.newPassword}
                autoComplete="new-password"
                helper={rotateCopy.helper}
                value={next}
                error={errors.next}
                onChange={(event) => {
                  setNext(event.target.value);
                  revalidate(event.target.value, confirm);
                }}
              />
              <PasswordRulesList rules={rules} />
              <PasswordField
                ref={confirmRef}
                id="rotate-confirm-password"
                label={rotateCopy.confirmPassword}
                autoComplete="new-password"
                value={confirm}
                error={errors.confirm}
                onChange={(event) => {
                  setConfirm(event.target.value);
                  revalidate(next, event.target.value);
                }}
              />
            </div>
            <div className={styles.actions}>
              <Button
                type="submit"
                variant="primary"
                size="large"
                fullWidth
                busy={busy}
                busyLabel={rotateCopy.busy}
              >
                {rotateCopy.submit}
              </Button>
              <Button
                variant="secondary"
                size="large"
                fullWidth
                onClick={() => {
                  backToSignIn();
                }}
              >
                {rotateCopy.back}
              </Button>
            </div>
          </form>
        </Card>
      </div>
    </main>
  );
}
