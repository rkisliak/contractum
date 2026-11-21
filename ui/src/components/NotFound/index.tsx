// NotFound (UI-SPEC 7.6, D-UI-08-25): every 404 on a detail route and every unknown URL. It never
// says forbidden and never leaks whether the item exists. It renders the shared page frame header.
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { Button } from "@/components/Button";
import { PageHeader } from "@/components/PageHeader";
import styles from "./NotFound.module.css";

export function NotFound({
  unknownUrl = false,
  back,
  nested = false,
}: {
  /** Inside a page that already has its frame (a tab or a drawer): an h2, no second header. */
  nested?: boolean;
  /** An unknown URL rather than a missing item. */
  unknownUrl?: boolean;
  /** "Back to {label}" when a parent exists. */
  back?: { label: string; to: string };
}) {
  const navigate = useNavigate();
  const block = useRef<HTMLElement>(null);
  useEffect(() => {
    if (nested) return;
    block.current?.querySelector<HTMLElement>("[data-page-heading]")?.focus();
  }, [nested]);
  return (
    <section ref={block} className={styles.block}>
      {nested ? (
        <h2 className={styles.heading}>Not found</h2>
      ) : (
        <PageHeader
          title="Not found"
          breadcrumbs={
            back
              ? [
                  {
                    label:
                      back.label.charAt(0).toUpperCase() + back.label.slice(1),
                    to: back.to,
                  },
                ]
              : []
          }
        />
      )}
      <p className={styles.lead}>
        {unknownUrl
          ? "This page does not exist."
          : "This item does not exist, or you do not have access to it."}
      </p>
      <div className={styles.actions}>
        <Button
          variant="primary"
          size="small"
          onClick={() => {
            void navigate({ to: "/" });
          }}
        >
          Go to home
        </Button>
        {back ? (
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              void navigate({ to: back.to });
            }}
          >
            Back to {back.label}
          </Button>
        ) : null}
      </div>
    </section>
  );
}
