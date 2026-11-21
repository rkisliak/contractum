// The two-column frame of the sign-in screens (quick 261121-0sh): the brand panel (wordmark,
// tagline and lead, no seal) beside a centred card column. The sign-in and Microsoft callback
// pages render their own Card inside it.
import type { ReactNode } from "react";
import { Wordmark } from "@/components/Logo";
import { signInCopy } from "./copy";
import styles from "./SignInPage.module.css";

export function SignInShell({ children }: { children: ReactNode }) {
  return (
    <div className={styles.page}>
      <aside className={styles.brand} aria-label="About Contractum">
        <p className={styles.lockup}>
          <span className={styles.lockupText}>
            <Wordmark className={styles.lockupWord} />
            <span className={styles.lockupTagline}>
              {signInCopy.brandTagline}
            </span>
          </span>
        </p>
        <p className={styles.lead}>{signInCopy.brandLead}</p>
      </aside>
      <main className={styles.main}>
        <div className={styles.card}>{children}</div>
      </main>
    </div>
  );
}
