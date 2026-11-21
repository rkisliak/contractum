// Card (UI-SPEC 5.10): a section with a hairline border, square, no shadow, spacing-6 padding.
import { useId } from "react";
import type { ReactNode } from "react";
import styles from "./Card.module.css";

export type CardProps = {
  title?: string;
  /** Heading level of the title; the sign-in and rotate cards use `h1`. */
  titleAs?: "h1" | "h2" | "h3";
  /** "page" is the 28/600 page heading used by the standalone sign-in and rotate cards. */
  titleSize?: "card" | "page";
  /** "band" puts the title in a grey header strip (table-head styling) for dashboard blocks. */
  header?: "plain" | "band";
  children: ReactNode;
  footer?: ReactNode;
};

export function Card({
  title,
  titleAs: Heading = "h2",
  titleSize = "card",
  header = "plain",
  children,
  footer,
}: CardProps) {
  const titleId = useId();
  return (
    <section
      className={[styles.card, header === "band" && title ? styles.banded : ""]
        .filter(Boolean)
        .join(" ")}
      data-card=""
      aria-labelledby={title ? titleId : undefined}
    >
      {title ? (
        <Heading
          id={titleId}
          className={[
            titleSize === "page" ? styles.titlePage : styles.title,
            header === "band" ? styles.band : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          {title}
        </Heading>
      ) : null}
      <div className={styles.body}>{children}</div>
      {footer ? <div className={styles.footer}>{footer}</div> : null}
    </section>
  );
}
