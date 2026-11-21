// Spinner (UI-SPEC 5.9, D-UI-08-29): a 16px rotating Loader for short indeterminate work. Under
// prefers-reduced-motion it is replaced by a static text label. The CSS also stops the rotation,
// so a change of preference without a remount is covered as well.
import { Loader } from "lucide-react";
import { useSyncExternalStore } from "react";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import styles from "./states.module.css";

const QUERY = "(prefers-reduced-motion: reduce)";

function subscribe(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") {
    return () => undefined;
  }
  const list = window.matchMedia(QUERY);
  list.addEventListener("change", onChange);
  return () => {
    list.removeEventListener("change", onChange);
  };
}

function getSnapshot(): boolean {
  return (
    typeof window.matchMedia === "function" && window.matchMedia(QUERY).matches
  );
}

export type SpinnerProps = {
  /** Status text; defaults to "Loading…". */
  label?: string;
};

export function Spinner({ label = "Loading…" }: SpinnerProps) {
  const reduced = useSyncExternalStore(subscribe, getSnapshot, () => false);
  if (reduced) {
    return (
      <span role="status" className={styles.spinnerLabel}>
        {label}
      </span>
    );
  }
  return (
    <span className={styles.spinner}>
      <Loader
        className={styles.spinnerIcon}
        size={16}
        strokeWidth={2}
        aria-hidden="true"
      />
      <VisuallyHidden role="status">{label}</VisuallyHidden>
    </span>
  );
}
