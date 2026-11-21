// The local sign-in page (UI-SPEC 7.1, D-UI-08-18). A standalone screen: a brand panel beside a
// form card (a top band under 1024px). Failures come back as typed Problems and render through
// ProblemAlert; a 401 is always the one generic message so the page never says whether the
// username exists (T-8-28).
import { useRouter } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import type { SubmitEvent } from "react";
import { isApiError, type Problem } from "@/api/problem";
import type { Notice } from "@/auth/notices";
import { safeRedirect } from "@/auth/redirect";
import type { Session } from "@/auth/session";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { PasswordField, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { noticeCopy, signInCopy } from "./copy";
import { SignInShell } from "./SignInShell";
import { useAuthPolicy } from "./passwordRules";
import styles from "./SignInPage.module.css";

export type SignInPageProps = {
  session: Session;
  /** Which sign-in methods the deployment offers (runtime config). */
  methods?: { local: boolean; entra: boolean };
  /** The `redirect` search param, untrusted. */
  redirect?: string | undefined;
  /** The validated `notice` search param. */
  notice?: Notice | undefined;
};

export function SignInPage({
  session,
  redirect,
  notice,
  methods: configured = { local: true, entra: false },
}: SignInPageProps) {
  const router = useRouter();
  // A method is offered only when the deployment's runtime config enables it AND the platform
  // policy (an admin can switch one off) says it is on. While the policy loads nothing is offered,
  // so a switched-off method never flashes; when the call fails the config alone decides.
  const policy = useAuthPolicy();
  const policyPending = policy.isPending;
  const methods = {
    local: configured.local && (policy.data?.methods.local ?? true),
    entra: configured.entra && (policy.data?.methods.microsoft ?? true),
  };
  const [entraBusy, setEntraBusy] = useState(false);
  const usernameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  // A submit that lands before React commits `busy` (a fast double Enter) is still ignored.
  const inFlight = useRef(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [usernameError, setUsernameError] = useState<string | undefined>();
  const [passwordError, setPasswordError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState(false);
  // Bumped after a failed sign-in clears the password; the effect focuses the cleared field.
  const [passwordFocusRequest, setPasswordFocusRequest] = useState(0);

  useEffect(() => {
    document.title = signInCopy.documentTitle;
  }, []);

  useEffect(() => {
    if (passwordFocusRequest > 0) passwordRef.current?.focus();
  }, [passwordFocusRequest]);

  async function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;

    const missingUsername = username.trim() === "";
    const missingPassword = password === "";
    setUsernameError(missingUsername ? signInCopy.usernameRequired : undefined);
    setPasswordError(missingPassword ? signInCopy.passwordRequired : undefined);
    if (missingUsername || missingPassword) {
      setProblem(null);
      (missingUsername ? usernameRef : passwordRef).current?.focus();
      return;
    }

    inFlight.current = true;
    setBusy(true);
    setProblem(null);
    try {
      const result = await session.signIn(username.trim(), password);
      if (result.kind === "signed-in") {
        // The busy state stays on while the router leaves this page.
        router.history.push(safeRedirect(redirect));
        return;
      }
      // A one-time bootstrap password: the restricted token stays in the session (UI-SPEC 7.3).
      void router.navigate({
        to: "/signin/rotate",
        search: redirect === undefined ? {} : { redirect },
      });
      return;
    } catch (error) {
      if (!isApiError(error)) throw error;
      setProblem(error.problem);
      if (error.problem.status === 401) {
        setPassword("");
        setPasswordFocusRequest((count) => count + 1);
      }
    }
    inFlight.current = false;
    setBusy(false);
  }

  return (
    <SignInShell>
      <Card title={signInCopy.heading} titleAs="h1" titleSize="page">
        {policyPending ? <Skeleton label="sign-in options" rows={2} /> : null}
        {!policyPending && !methods.local && !methods.entra ? (
          <Alert intent="danger">{signInCopy.noMethod}</Alert>
        ) : null}
        {!policyPending && !methods.local && notice !== undefined ? (
          <Alert intent="info">{noticeCopy[notice]}</Alert>
        ) : null}
        {!policyPending && methods.local ? (
          <form
            className={styles.form}
            noValidate
            onSubmit={(event) => {
              void handleSubmit(event);
            }}
          >
            {notice === undefined ? null : (
              <Alert intent="info">{noticeCopy[notice]}</Alert>
            )}
            {problem ? (
              <ProblemAlert problem={problem} context="signin" />
            ) : null}
            <div className={styles.fields}>
              <TextField
                ref={usernameRef}
                id="signin-username"
                label={signInCopy.username}
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                value={username}
                error={usernameError}
                onChange={(event) => {
                  setUsername(event.target.value);
                }}
              />
              <PasswordField
                ref={passwordRef}
                id="signin-password"
                label={signInCopy.password}
                autoComplete="current-password"
                value={password}
                error={passwordError}
                onChange={(event) => {
                  setPassword(event.target.value);
                }}
              />
            </div>
            <Button
              type="submit"
              variant="primary"
              size="large"
              fullWidth
              busy={busy}
              busyLabel="Signing in…"
            >
              {signInCopy.submit}
            </Button>
          </form>
        ) : null}
        {!policyPending && methods.entra ? (
          <div className={styles.entra}>
            {methods.local ? (
              <p className={styles.divider}>
                <span>{signInCopy.divider}</span>
              </p>
            ) : null}
            <Button
              variant="outline-neutral"
              size="large"
              fullWidth
              busy={entraBusy}
              busyLabel="Redirecting…"
              onClick={() => {
                setEntraBusy(true);
                // The redirect starts on /auth/callback so the bridge returns there (A-4).
                void router.navigate({
                  to: "/auth/callback",
                  search: {
                    start: "1",
                    ...(redirect === undefined ? {} : { redirect }),
                  },
                });
              }}
            >
              {signInCopy.microsoft}
            </Button>
          </div>
        ) : null}
      </Card>
    </SignInShell>
  );
}
