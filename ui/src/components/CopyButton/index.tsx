// CopyButton (UI-SPEC 5.4, Copywriting Contract): a ghost Small button that copies `value`,
// swaps its label for the confirmation for 3 s and repeats the confirmation in a polite live
// region. The live region only ever holds `copiedLabel`, never the copied value (T-8-47).
import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/Button";
import { VisuallyHidden } from "@/components/VisuallyHidden";

const CONFIRMATION_MS = 3000;

export type CopyButtonProps = {
  value: string;
  /** For example "Copy request ID". */
  label: string;
  /** For example "Request ID copied". */
  copiedLabel: string;
  /** What the polite live region says; defaults to `copiedLabel` ("Token copied to clipboard"). */
  announcement?: string;
  /** Ghost in tables and alerts; Secondary beside a field (SecretRevealDialog). */
  variant?: "ghost" | "secondary";
  /** Called when the clipboard is unavailable or rejects; the label stays unchanged. */
  onError?: (error: unknown) => void;
};

export function CopyButton({
  value,
  label,
  copiedLabel,
  announcement = copiedLabel,
  variant = "ghost",
  onError,
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(
    () => () => {
      clearTimeout(timer.current);
    },
    [],
  );

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
    } catch (error) {
      onError?.(error);
      return;
    }
    setCopied(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      setCopied(false);
    }, CONFIRMATION_MS);
  }

  return (
    <>
      <Button
        variant={variant}
        size="small"
        icon={copied ? Check : Copy}
        onClick={() => {
          void copy();
        }}
      >
        {copied ? copiedLabel : label}
      </Button>
      {/* The region exists before the text changes so the change is announced. */}
      <VisuallyHidden role="status" aria-live="polite">
        {copied ? announcement : ""}
      </VisuallyHidden>
    </>
  );
}
