// NotAvailable: the calm state for an area or section the caller's access does not cover (a read
// the capabilities rule out). It is an EmptyState, never an error: a normal permission situation
// is not a problem to report. The API still enforces every request; this only avoids asking.
import { Lock } from "lucide-react";
import { EmptyState } from "./EmptyState";

export function NotAvailable({
  what,
  level = 2,
}: {
  /** What is not available, as a sentence start ("The drift report for this folder"). */
  what: string;
  level?: 2 | 3 | 4;
}) {
  return (
    <EmptyState
      level={level}
      icon={Lock}
      heading="Not available at your access level"
      body={`${what} is not included in your access. Ask a folder admin if you need it.`}
    />
  );
}
