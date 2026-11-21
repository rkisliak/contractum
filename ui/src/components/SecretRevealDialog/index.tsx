// SecretRevealDialog (UI-SPEC 5.5, D-UI-12-12): the shared show-once dialog for tokens, service
// account keys and webhook secrets. It cannot be dismissed by Esc, a backdrop click or a close
// button; it ends only through "Close after storing {kind}", which stays natively disabled until
// the acknowledgement box is ticked. The secret is read from props and rendered, and
// is held nowhere else: not state, the query cache, storage, the URL or a log (T-8-29). After
// the close action the dialog renders nothing at all, so the text leaves the DOM at once.
import { useId, useRef, useState } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { CopyButton } from "@/components/CopyButton";
import { Dialog } from "@/components/Dialog";
import { Checkbox } from "@/components/form";
import styles from "./SecretRevealDialog.module.css";

export type SecretKind = "token" | "key" | "webhook secret";

export type SecretRevealDialogProps = {
  open: boolean;
  kind: SecretKind;
  /** The value to show once. The caller clears its own copy in `onClosed`. */
  secret: string;
  /** Called once, when the user closes the dialog after storing the secret. */
  onClosed: () => void;
};

function capitalised(kind: SecretKind): string {
  return `${kind.charAt(0).toUpperCase()}${kind.slice(1)}`;
}

/** The field grows to show the whole secret, up to six lines, then scrolls. */
function rowsFor(secret: string): number {
  return Math.min(6, Math.max(2, Math.ceil(secret.length / 44)));
}

export function SecretRevealDialog({
  open,
  kind,
  secret,
  onClosed,
}: SecretRevealDialogProps) {
  // Mounted only while open: closing and reopening starts from a clean, unticked state.
  if (!open) return null;
  return <SecretReveal kind={kind} secret={secret} onClosed={onClosed} />;
}

function SecretReveal({
  kind,
  secret,
  onClosed,
}: Omit<SecretRevealDialogProps, "open">) {
  const ids = useId();
  const fieldId = `${ids}-secret`;
  const checkboxId = `${ids}-stored`;
  const helperId = `${ids}-helper`;
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  const [stored, setStored] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [closed, setClosed] = useState(false);

  if (closed) return null;

  const Kind = capitalised(kind);

  function selectField() {
    fieldRef.current?.focus();
    fieldRef.current?.select();
  }

  return (
    <Dialog
      open
      // Nothing outside the close action ends this dialog (dismissible is false).
      onOpenChange={() => undefined}
      dismissible={false}
      size={512}
      title={`Your new ${kind}`}
      description={
        <Alert intent="warning">
          This is the only time this {kind} is shown. Store it in your secret
          manager now. Copying does not clear your clipboard automatically.
        </Alert>
      }
      footer={
        <>
          {stored ? null : (
            <p id={helperId} className={styles.helper}>
              Tick the box to enable closing.
            </p>
          )}
          <Button
            variant="primary"
            size="small"
            disabled={!stored}
            aria-describedby={stored ? undefined : helperId}
            onClick={() => {
              setClosed(true);
              onClosed();
            }}
          >
            Close after storing {kind}
          </Button>
        </>
      }
    >
      <div className={styles.fieldGroup}>
        <label htmlFor={fieldId} className={styles.fieldLabel}>
          {Kind}
        </label>
        <textarea
          ref={fieldRef}
          id={fieldId}
          className={styles.field}
          readOnly
          rows={rowsFor(secret)}
          value={secret}
          spellCheck={false}
          autoComplete="off"
          onFocus={(event) => {
            event.currentTarget.select();
          }}
        />
        <div className={styles.copyRow}>
          <CopyButton
            variant="secondary"
            value={secret}
            label={`Copy ${kind}`}
            copiedLabel={`${Kind} copied`}
            announcement={`${Kind} copied to clipboard`}
            onError={() => {
              setCopyFailed(true);
              selectField();
            }}
          />
        </div>
        {copyFailed ? (
          <Alert intent="danger">
            Copy failed. Select the {kind} and copy it manually.
          </Alert>
        ) : null}
      </div>
      <Checkbox
        id={checkboxId}
        label={`I have stored this ${kind}`}
        checked={stored}
        onChange={(event) => {
          setStored(event.target.checked);
        }}
      />
    </Dialog>
  );
}
