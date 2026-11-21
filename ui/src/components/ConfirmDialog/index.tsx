// ConfirmDialog (D-UI-08-27 as amended, D-UI-08-26, UI-SPEC 5.5): the destructive-action pattern
// for Phases 9 to 13. Radix AlertDialog, 448px, static backdrop. Initial focus is the dismiss
// button ("Keep {object}", or "Go back" when no object applies; never "Cancel"), the confirm is
// Danger and carries the verb and object, and an irreversible action adds a "Type {name} to
// confirm" field that keeps the confirm natively disabled until the name matches exactly. While
// the action runs the confirm is busy; on failure the dialog stays open and shows the problem.
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import { useId, useRef, useState } from "react";
import type { ReactNode, SyntheticEvent } from "react";
import { GENERIC_TITLE, isApiError, type Problem } from "@/api/problem";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import dialogStyles from "@/components/Dialog/Dialog.module.css";
import { TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { cx } from "@/lib/cx";
import styles from "./ConfirmDialog.module.css";

export type ConfirmDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Names the action and the object: "Archive Payments?". */
  title: string;
  /** The effect, in one or two sentences. */
  body: ReactNode;
  /** The object's name; the dismiss button reads "Keep {objectName}". */
  objectName?: string;
  /** The verb and object: "Archive Payments". */
  confirmLabel: string;
  /** Overrides the derived "Keep {objectName}" / "Go back". Never "Cancel". */
  dismissLabel?: string;
  /** The exact name to type before the confirm enables (irreversible actions). */
  typeToConfirm?: string;
  /** The verb alone, for "Type the name exactly to enable {verb}."; defaults to the first word of `confirmLabel`. */
  verb?: string;
  /** The active verb plus an ellipsis ("Archiving…"); defaults to "Working…". */
  busyLabel?: string;
  /**
   * Runs the action. Resolving closes the dialog; rejecting keeps it open and shows the problem
   * (an `ApiError` renders its Problem, anything else a generic message).
   */
  onConfirm: () => Promise<void>;
};

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  body,
  objectName,
  confirmLabel,
  dismissLabel,
  typeToConfirm,
  verb,
  busyLabel = "Working…",
  onConfirm,
}: ConfirmDialogProps) {
  const fieldId = useId();
  const opener = useRef<HTMLElement | null>(null);
  const [pending, setPending] = useState(false);
  const [typed, setTyped] = useState("");
  const [failure, setFailure] = useState<Problem | "generic" | null>(null);

  // A closed dialog starts fresh next time (adjusting state while rendering, not in an effect).
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (!open) {
      setTyped("");
      setFailure(null);
    }
  }

  const dismiss =
    dismissLabel ?? (objectName ? `Keep ${objectName}` : "Go back");
  const verbText = verb ?? confirmLabel.split(" ")[0] ?? confirmLabel;
  const matches = typeToConfirm === undefined || typed === typeToConfirm;

  function handleOpenChange(next: boolean) {
    // The action is running: closing now would hide its result.
    if (!next && pending) return;
    onOpenChange(next);
  }

  async function run(event: SyntheticEvent) {
    event.preventDefault();
    if (pending || !matches) return;
    setPending(true);
    setFailure(null);
    try {
      await onConfirm();
    } catch (error) {
      setFailure(isApiError(error) ? error.problem : "generic");
      return;
    } finally {
      setPending(false);
    }
    onOpenChange(false);
  }

  return (
    <AlertDialog.Root open={open} onOpenChange={handleOpenChange}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className={dialogStyles.overlay} />
        <AlertDialog.Content
          className={cx(dialogStyles.content, dialogStyles.size448)}
          onOpenAutoFocus={() => {
            // Radix then focuses the Cancel (dismiss) button; remember who opened the dialog.
            opener.current =
              document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            opener.current?.focus();
          }}
        >
          <div className={dialogStyles.header}>
            <AlertDialog.Title className={dialogStyles.title}>
              {title}
            </AlertDialog.Title>
          </div>
          <form
            className={styles.form}
            onSubmit={(event) => {
              void run(event);
            }}
          >
            <div className={dialogStyles.body}>
              <AlertDialog.Description asChild>
                <div className={dialogStyles.description}>{body}</div>
              </AlertDialog.Description>
              {failure === "generic" ? (
                <Alert intent="danger" title={GENERIC_TITLE}>
                  <p>The action did not complete. Try again.</p>
                </Alert>
              ) : failure ? (
                <ProblemAlert problem={failure} context="action" />
              ) : null}
              {typeToConfirm !== undefined ? (
                <TextField
                  id={fieldId}
                  label={`Type ${typeToConfirm} to confirm`}
                  helper={`Type the name exactly to enable ${verbText}.`}
                  autoComplete="off"
                  spellCheck={false}
                  value={typed}
                  onChange={(event) => {
                    setTyped(event.target.value);
                  }}
                />
              ) : null}
            </div>
            <div className={dialogStyles.footer}>
              <AlertDialog.Cancel asChild>
                <Button variant="secondary" size="small" disabled={pending}>
                  {dismiss}
                </Button>
              </AlertDialog.Cancel>
              <Button
                variant="danger"
                size="small"
                type="submit"
                busy={pending}
                busyLabel={busyLabel}
                disabled={!matches}
              >
                {confirmLabel}
              </Button>
            </div>
          </form>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
