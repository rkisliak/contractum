// Create a PAT or a service-account key (D-UI-12-10, D-UI-12-11): a name, an expiry date (default
// 30 or 90 days, capped at the server maximum) and, for PATs, "Read-only". The create call is made
// directly, not through useMutation, so the show-once token never enters a TanStack cache; it is
// handed to `onCreated`, whose caller passes it to SecretRevealDialog and clears it on close.
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import { Button } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { Checkbox, nameRuleError, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { usePublicSettings } from "@/features/settings/api";
import { problemOf } from "@/lib/unexpected";
import { dateInDays, expiresAtFor, MAX_CREDENTIAL_DAYS } from "./shared";
import styles from "./admin.module.css";

export type CredentialInput = {
  name: string;
  expiresAt: string;
  readOnly: boolean;
};

export function CreateCredentialDialog({
  open,
  onOpenChange,
  title,
  description,
  submitLabel,
  defaultDays,
  withReadOnly = false,
  create,
  onCreated,
  refusalCopy = {},
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  submitLabel: string;
  defaultDays: number;
  withReadOnly?: boolean;
  /** Calls the API and resolves with the one-time token. */
  create: (input: CredentialInput) => Promise<string>;
  onCreated: (token: string) => void;
  /** Fixed copy for known problem slugs (shown instead of the API detail). */
  refusalCopy?: Record<string, string>;
}) {
  // The ceiling is the stored setting (the default expiry never exceeds it); 365 until it is read.
  const maxDays =
    usePublicSettings().data?.credential_max_ttl_days ?? MAX_CREDENTIAL_DAYS;
  const startDays = Math.min(defaultDays, maxDays);
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState(() => dateInDays(startDays));
  const [readOnly, setReadOnly] = useState(false);
  const [errors, setErrors] = useState<{ name?: string; expiry?: string }>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState(false);

  function reset() {
    setName("");
    setExpiry(dateInDays(startDays));
    setReadOnly(false);
    setErrors({});
    setProblem(null);
  }

  async function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const found: { name?: string; expiry?: string } = {};
    if (name.trim() === "") found.name = "Enter a name.";
    else found.name = nameRuleError(name);
    const expiresAt = expiresAtFor(expiry, new Date(), maxDays);
    if (expiresAt === null)
      found.expiry = `Choose a date from tomorrow up to ${String(maxDays)} days from today.`;
    setErrors(found);
    setProblem(null);
    if (found.name || expiresAt === null) return;
    setBusy(true);
    try {
      const token = await create({ name: name.trim(), expiresAt, readOnly });
      reset();
      onOpenChange(false);
      onCreated(token);
    } catch (error) {
      const p = problemOf(error);
      if (p.kind === "problem" && p.slug === "expiry-out-of-range") {
        setErrors({ expiry: p.detail ?? "This expiry is out of range." });
      } else if (
        p.kind === "problem" &&
        p.status === 409 &&
        !p.slug?.startsWith("credential") &&
        p.slug !== "token-limit-reached"
      ) {
        setErrors({ name: "A credential with this name already exists." });
      } else {
        const copy =
          p.kind === "problem" && p.slug ? refusalCopy[p.slug] : undefined;
        setProblem(copy && p.kind === "problem" ? { ...p, detail: copy } : p);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title={title}
      description={description}
      footer={
        <>
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              reset();
              onOpenChange(false);
            }}
          >
            Go back
          </Button>
          <Button
            variant="primary"
            size="small"
            type="submit"
            form="create-credential-form"
            busy={busy}
            busyLabel="Creating…"
          >
            {submitLabel}
          </Button>
        </>
      }
    >
      <form
        id="create-credential-form"
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          void onSubmit(event);
        }}
      >
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <TextField
          id="credential-name"
          label="Name"
          autoComplete="off"
          value={name}
          error={errors.name ?? nameRuleError(name)}
          maxLength={100}
          helper="Say where it is used, for example the pipeline name."
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
        <TextField
          id="credential-expiry"
          label="Expires on"
          type="date"
          autoComplete="off"
          value={expiry}
          min={dateInDays(1)}
          max={dateInDays(maxDays)}
          error={errors.expiry}
          helper={`Default ${String(startDays)} days, at most ${String(maxDays)}.`}
          onChange={(event) => {
            setExpiry(event.target.value);
          }}
        />
        {withReadOnly ? (
          <Checkbox
            id="credential-read-only"
            label="Read-only"
            helper="Can only read; every write is refused."
            checked={readOnly}
            onChange={(event) => {
              setReadOnly(event.target.checked);
            }}
          />
        ) : null}
      </form>
    </Dialog>
  );
}
