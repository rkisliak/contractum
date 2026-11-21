// /auth/callback (UI-SPEC 7.2, D-UI-08-16, A-4): the Entra redirect landing.
//
// `?start=1` starts the MSAL redirect from this route, so the bridge page (/redirect.html) brings
// the browser back here. Without it the page completes the redirect: the access token goes into
// the shared token store, `GET /me` proves the platform admits the principal (an unregistered
// Entra user gets the API's 403 here, not a broken shell), then the user goes where they were
// heading. Progress is a focused status line with a 10 s "taking longer" backstop; any failure
// ends on a card with the problem, "Try loading again" and "Back to sign in". No token, code or
// claim is ever rendered (T-8-42).
import { useQueryClient } from "@tanstack/react-query";
import { useRouteContext, useRouter } from "@tanstack/react-router";
import { LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { isApiError, type Problem } from "@/api/problem";
import {
  completeEntraSignIn,
  startEntraSignIn,
  type EntraCompletion,
} from "@/auth/msal";
import { safeRedirect } from "@/auth/redirect";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { ProblemAlert } from "@/components/ProblemAlert";
import type { RuntimeConfig } from "@/config";
import { callbackCopy } from "./copy";
import { SignInShell } from "./SignInShell";
import styles from "./SignInPage.module.css";

export type CallbackSearch = { start?: "1"; redirect?: string };

const SLOW_AFTER_MS = 10_000;

function fallbackProblem(): Problem {
  return {
    kind: "problem",
    status: 401,
    slug: null,
    title: callbackCopy.failedTitle,
    detail: callbackCopy.failedDetail,
    errors: [],
    requestId: null,
  };
}

// StrictMode runs effects twice in development, and MSAL must see one handleRedirectPromise per
// response: the completion is shared for the page's lifetime.
let completion: Promise<EntraCompletion | null> | null = null;

/** Test seam: forget the shared completion. */
export function resetCallbackForTests(): void {
  completion = null;
}

function completeOnce(config: RuntimeConfig) {
  completion ??= completeEntraSignIn(config);
  return completion;
}

export function CallbackPage({ search }: { search: CallbackSearch }) {
  const { config, session, api } = useRouteContext({ from: "__root__" });
  const router = useRouter();
  const queryClient = useQueryClient();
  const statusRef = useRef<HTMLParagraphElement>(null);
  const alertRef = useRef<HTMLDivElement>(null);
  const [problem, setProblem] = useState<Problem | null>(() =>
    config.entraEnabled ? null : fallbackProblem(),
  );
  const [slow, setSlow] = useState(false);
  const started = useRef(false);

  useEffect(() => {
    document.title = callbackCopy.documentTitle;
    statusRef.current?.focus();
    const timer = setTimeout(() => {
      setSlow(true);
    }, SLOW_AFTER_MS);
    return () => {
      clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    if (problem) alertRef.current?.focus();
  }, [problem]);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    if (!config.entraEnabled) return;
    if (search.start === "1") {
      startEntraSignIn(config, search.redirect).catch(() => {
        setProblem(fallbackProblem());
      });
      return;
    }
    void (async () => {
      try {
        const result = await completeOnce(config);
        if (result === null) {
          setProblem(fallbackProblem());
          return;
        }
        // The platform decides whether this principal may use it (registration, tenant). The
        // token is checked before it becomes the session: a caller-set Authorization skips the
        // 401 refresh-and-retry, so a refused token lands here, not on "session expired".
        const { data } = await api.GET("/api/v1/identity/me", {
          headers: { Authorization: `Bearer ${result.accessToken}` },
        });
        session.adoptEntraToken(result.accessToken, result.expiresInS);
        if (data) queryClient.setQueryData(["me"], data);
        router.history.push(safeRedirect(result.redirect));
      } catch (error) {
        setProblem(isApiError(error) ? error.problem : fallbackProblem());
      }
    })();
  }, [
    api,
    config,
    queryClient,
    router,
    search.redirect,
    search.start,
    session,
  ]);

  function backToSignIn() {
    void router.navigate({ to: "/signin" });
  }

  function retry() {
    setProblem(null);
    setSlow(false);
    completion = null;
    startEntraSignIn(config, search.redirect).catch(() => {
      setProblem(fallbackProblem());
    });
  }

  return (
    <SignInShell>
      <Card title="Sign in with Microsoft" titleAs="h1" titleSize="page">
        {problem ? (
          <div className={styles.form}>
            <div ref={alertRef} tabIndex={-1}>
              <ProblemAlert problem={problem} />
            </div>
            <div className={styles.callbackActions}>
              <Button variant="primary" size="small" onClick={retry}>
                {callbackCopy.retry}
              </Button>
              <Button variant="secondary" size="small" onClick={backToSignIn}>
                {callbackCopy.back}
              </Button>
            </div>
          </div>
        ) : (
          <div className={styles.form}>
            <p
              ref={statusRef}
              tabIndex={-1}
              role="status"
              className={styles.callbackStatus}
            >
              <LoaderCircle size={16} aria-hidden="true" />
              {callbackCopy.progress}
            </p>
            {slow ? (
              <>
                <p className={styles.callbackStatus}>{callbackCopy.slow}</p>
                <div className={styles.callbackActions}>
                  <Button
                    variant="secondary"
                    size="small"
                    onClick={backToSignIn}
                  >
                    {callbackCopy.back}
                  </Button>
                </div>
              </>
            ) : null}
          </div>
        )}
      </Card>
    </SignInShell>
  );
}
