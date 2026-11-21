// Logo: the Contractum seal (a rosette with a check) and the one-word Saira wordmark
// ("Contractum"). `tone` picks the fills for the surface it sits on: "light"
// for the app's light and dark themed panels (the brand ramp), "rail" for the always-dark slate
// panels (sign-in brand panel), where the top layer turns white so the mark keeps its contrast.
import { cx } from "@/lib/cx";
import { LogoGlyph } from "./LogoGlyph";
import styles from "./Logo.module.css";

type Tone = "light" | "rail";

export function LogoMark({
  size = 28,
  tone = "light",
  className,
}: {
  /** Edge in px (28 sidebar, 32 top bar, boot screen and sign-in). */
  size?: number;
  tone?: Tone;
  className?: string;
}) {
  return (
    <svg
      className={cx(styles.mark, styles[tone], className)}
      viewBox="0 0 32 32"
      width={size}
      height={size}
      focusable="false"
      aria-hidden="true"
    >
      <LogoGlyph />
    </svg>
  );
}

/** "Contractum" in Saira 700; one phrase for assistive tech. */
export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cx(styles.wordmark, className)}>
      <span className={styles.wordStrong}>Contractum</span>
    </span>
  );
}

/** The mark plus the wordmark; the text is the accessible name, the mark is decorative. */
export function Logo({
  size = 28,
  tone = "light",
  className,
}: {
  size?: number;
  tone?: Tone;
  className?: string;
}) {
  return (
    <span className={cx(styles.logo, className)}>
      <LogoMark size={size} tone={tone} />
      <Wordmark />
    </span>
  );
}
