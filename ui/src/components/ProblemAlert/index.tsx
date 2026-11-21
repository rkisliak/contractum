// ProblemAlert (UI-SPEC 5.4, D-UI-08-24, D-UI-08-25): the one renderer for an RFC 9457 Problem.
// Title (600), detail (400), the `errors[]` list capped at five with a disclosure, the slug badge
// and the Request ID plus its copy control, then an optional Retry loading action. The slug and
// Request ID are for support: a server fault (5xx) shows them, every other problem (a wrong
// password, a validation or permission refusal, a conflict) keeps them in a collapsed "Technical
// details" so the message itself stays calm and short.
// Danger by default; 503, 429 and network failures are Warning with the fixed copy. Everything
// server-supplied is rendered as React text nodes (T-8-19); raw JSON is never shown (T-8-18).
import { ChevronDown } from "lucide-react";
import { useState } from "react";
import { Alert, type AlertIntent } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { CopyButton } from "@/components/CopyButton";
import { cx } from "@/lib/cx";
import {
  copyFor,
  type Problem,
  type ProblemContext,
  type ProblemFieldError,
} from "@/api/problem";
import styles from "./ProblemAlert.module.css";

const COLLAPSED_ERRORS = 5;

export type ProblemAlertProps = {
  problem: Problem;
  /** Selects the fixed wording (signin 401, action 403). Omitted: the API's own title and detail. */
  context?: ProblemContext;
  /** Renders the "Retry loading" action when given. */
  onRetry?: () => void;
  /** JSON Pointer to the field label shown in the errors list, for example "/new_password". */
  fieldLabels?: Record<string, string>;
  className?: string;
};

function intentFor(problem: Problem): AlertIntent {
  if (problem.kind === "network") return "warning";
  return problem.status === 503 || problem.status === 429
    ? "warning"
    : "danger";
}

// The sentence that follows a fixed headline when the API gave none of its own.
function supplementFor(problem: Problem): string | null {
  if (problem.kind === "network") {
    return "Check your connection and try again.";
  }
  return problem.status === 503 ? "Try again in a moment." : null;
}

export function ProblemAlert({
  problem,
  context,
  onRetry,
  fieldLabels,
  className,
}: ProblemAlertProps) {
  const [expanded, setExpanded] = useState(false);
  // Fixed wording from the Copywriting Contract. Without a context only the classes that read
  // the same everywhere apply (network, 503, 429); signin 401 and action 403 need a context.
  const fixed = copyFor(problem, context ?? "page");

  const title = fixed ?? (problem.kind === "problem" ? problem.title : "");
  const detail =
    problem.kind === "problem"
      ? // The API's detail is kept next to a fixed headline only for a 403 action (it can name
        // the missing permission); for the throttle, 503 and sign-in copy it would repeat it.
        fixed === null || (problem.status === 403 && context === "action")
        ? problem.detail
        : (supplementFor(problem) ?? null)
      : supplementFor(problem);
  const errors = problem.kind === "problem" ? problem.errors : [];
  const slug = problem.kind === "problem" ? problem.slug : null;
  const requestId = problem.requestId;
  // Only a server fault needs the support details up front; the user cannot fix it themselves.
  const serverFault = problem.kind === "problem" && problem.status >= 500;

  const visible = expanded ? errors : errors.slice(0, COLLAPSED_ERRORS);
  const hidden = errors.length - COLLAPSED_ERRORS;

  return (
    <Alert
      intent={intentFor(problem)}
      title={title}
      className={className}
      action={
        onRetry ? (
          <Button variant="secondary" size="small" onClick={onRetry}>
            Retry loading
          </Button>
        ) : undefined
      }
    >
      {detail ? <p className={styles.detail}>{detail}</p> : null}
      {errors.length > 0 ? (
        <>
          <ul className={styles.errors}>
            {visible.map((error, index) => (
              <ErrorRow
                key={`${error.pointer}:${String(index)}`}
                error={error}
                label={fieldLabels?.[error.pointer]}
              />
            ))}
          </ul>
          {hidden > 0 ? (
            <Button
              variant="ghost"
              size="small"
              aria-expanded={expanded}
              onClick={() => {
                setExpanded((value) => !value);
              }}
            >
              {expanded
                ? "Show fewer errors"
                : hidden === 1
                  ? "Show 1 more error"
                  : `Show ${String(hidden)} more errors`}
            </Button>
          ) : null}
        </>
      ) : null}
      {slug || requestId ? (
        serverFault ? (
          <Meta slug={slug} requestId={requestId} />
        ) : (
          <details className={styles.details}>
            <summary className={styles.detailsSummary}>
              <span>Technical details</span>
              <ChevronDown
                size={14}
                strokeWidth={2}
                aria-hidden="true"
                className={styles.chevron}
              />
            </summary>
            <Meta slug={slug} requestId={requestId} />
          </details>
        )
      ) : null}
    </Alert>
  );
}

function Meta({
  slug,
  requestId,
}: {
  slug: string | null;
  requestId: string | null | undefined;
}) {
  return (
    <div className={styles.meta}>
      {slug ? (
        <Badge intent="neutral" mono>
          {slug}
        </Badge>
      ) : null}
      {requestId ? (
        <span className={styles.requestId}>
          <span className={styles.requestIdLabel}>Request ID</span>
          <span className={styles.requestIdValue}>{requestId}</span>
          <CopyButton
            value={requestId}
            label="Copy request ID"
            copiedLabel="Request ID copied"
          />
        </span>
      ) : null}
    </div>
  );
}

function ErrorRow({
  error,
  label,
}: {
  error: ProblemFieldError;
  label: string | undefined;
}) {
  return (
    <li className={styles.error}>
      {label ? (
        <span className={styles.errorLabel}>{label}</span>
      ) : (
        <code className={cx(styles.errorLabel, styles.pointer)}>
          {error.pointer || "(request)"}
        </code>
      )}{" "}
      <span>{error.detail}</span>
    </li>
  );
}
