// First paint before /config.json resolves, and the failure state when it cannot load
// (UI-SPEC 7.8). Styled from tokens only (Plan 04).
import { TriangleAlert } from "lucide-react";
import { LogoMark, Wordmark } from "@/components/Logo";
import styles from "./BootScreen.module.css";

type BootScreenProps = {
  state?: "loading" | "failed";
  onRetry?: () => void;
};

export function BootScreen({ state = "loading", onRetry }: BootScreenProps) {
  if (state === "failed") {
    return (
      <main className={styles.screen}>
        <p className={styles.mark}>
          <LogoMark size={32} />
          <Wordmark />
        </p>
        <div className={styles.alert}>
          <TriangleAlert
            className={styles.icon}
            size={16}
            strokeWidth={2}
            aria-hidden="true"
          />
          <div className={styles.alertBody}>
            <h1 className={styles.title}>Can&apos;t reach the server</h1>
            <p className={styles.message}>
              Check your connection and try again.
            </p>
          </div>
        </div>
        <button type="button" className={styles.button} onClick={onRetry}>
          Try loading again
        </button>
      </main>
    );
  }
  return (
    <main className={styles.screen}>
      <h1 className={styles.mark}>
        <LogoMark size={32} />
        <Wordmark />
      </h1>
      <p className={styles.status} role="status">
        Loading
      </p>
    </main>
  );
}
