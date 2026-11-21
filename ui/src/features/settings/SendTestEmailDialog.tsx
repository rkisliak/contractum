// Send a test email (quick 261120-0yd, N3): one fixed message to one address, through the mail
// service with the admin's own sign-in, from the SAVED sender. The outcome stays in the dialog as
// an information alert (sent) or a warning (refused, unavailable, not configured, rate limited).
// The address is only ever sent to the API; nothing here stores or logs it.
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import type { TestEmail } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { problemOf } from "@/lib/unexpected";
import { useSendTestEmail } from "./api";
import styles from "./settings.module.css";

const OUTCOME_COPY: Record<
  TestEmail["outcome"],
  { intent: "info" | "warning"; title: string; text: string }
> = {
  sent: {
    intent: "info",
    title: "Test email sent",
    text: "The mail server accepted the message. Check the inbox of the address you entered.",
  },
  refused: {
    intent: "warning",
    title: "Test email refused",
    text: "The mail service or the mail server refused the message. Check the address and the mail server settings.",
  },
  unavailable: {
    intent: "warning",
    title: "Mail is unavailable",
    text: "The mail service could not deliver the message right now. Try again shortly.",
  },
  "not-configured": {
    intent: "warning",
    title: "Email is not configured",
    text: "The mail service address or key is missing on this deployment.",
  },
};

function waitText(problem: Problem): string {
  const wait =
    problem.kind === "problem" ? problem.extensions?.retry_after_s : undefined;
  if (typeof wait !== "number" || wait < 1) return "later";
  return wait >= 120
    ? `in about ${String(Math.ceil(wait / 60))} minutes`
    : `in ${String(Math.ceil(wait))} seconds`;
}

export function SendTestEmailDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const send = useSendTestEmail();
  const [to, setTo] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [result, setResult] = useState<TestEmail | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);

  function reset() {
    setTo("");
    setError(undefined);
    setResult(null);
    setProblem(null);
    send.reset();
  }

  function submit() {
    const address = to.trim();
    setResult(null);
    setProblem(null);
    if (address === "" || !address.includes("@")) {
      setError("Enter an email address such as name@example.com.");
      return;
    }
    setError(undefined);
    send.mutate(address, {
      onSuccess: setResult,
      onError: (failure) => {
        const found = problemOf(failure);
        const own =
          found.kind === "problem" && found.status === 422
            ? found.errors.find((e) => e.pointer === "/to")
            : undefined;
        if (own) setError(own.detail);
        else setProblem(found);
      },
    });
  }

  const copy = result === null ? undefined : OUTCOME_COPY[result.outcome];
  const limited =
    problem?.kind === "problem" && problem.status === 429 ? problem : null;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Send a test email"
      description="Sends one short message through the mail service, from the saved sender. Save your changes first to test them."
      footer={
        <>
          <Button
            variant="outline-neutral"
            size="small"
            onClick={() => {
              onOpenChange(false);
              reset();
            }}
          >
            Close
          </Button>
          <Button
            variant="primary"
            size="small"
            busy={send.isPending}
            busyLabel="Sending…"
            onClick={submit}
          >
            Send test email
          </Button>
        </>
      }
    >
      <form
        className={styles.stack}
        noValidate
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          submit();
        }}
      >
        <TextField
          id="settings-test-email-to"
          label="Send to"
          type="email"
          autoComplete="off"
          value={to}
          error={error}
          onChange={(event) => {
            setTo(event.target.value);
            setError(undefined);
          }}
        />
        {copy ? (
          <Alert intent={copy.intent} title={copy.title}>
            {copy.text}
          </Alert>
        ) : null}
        {limited ? (
          <Alert intent="warning" title="Too many test emails">
            At most 5 test emails per 10 minutes. Try again {waitText(limited)}.
          </Alert>
        ) : problem ? (
          <ProblemAlert problem={problem} context="action" />
        ) : null}
      </form>
    </Dialog>
  );
}
