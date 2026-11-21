// How a failed settings save reads: a 412 is "changed elsewhere" with a reload (a warning, not a
// red alert); every other problem goes through ProblemAlert with the field errors that did not
// land on a field.
import type { Problem } from "@/api/problem";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { ProblemAlert } from "@/components/ProblemAlert";

export function isStale(problem: Problem): boolean {
  return problem.kind === "problem" && problem.status === 412;
}

export function SaveProblem({
  problem,
  onReload,
  fieldLabels,
}: {
  problem: Problem;
  onReload: () => void;
  fieldLabels?: Record<string, string>;
}) {
  if (isStale(problem)) {
    return (
      <Alert
        intent="warning"
        title="These settings changed elsewhere"
        action={
          <Button variant="outline-neutral" size="small" onClick={onReload}>
            Reload settings
          </Button>
        }
      >
        Someone else saved this section after you opened it. Reload to see the
        current values, then make your change again.
      </Alert>
    );
  }
  return (
    <ProblemAlert
      problem={problem}
      context="action"
      fieldLabels={fieldLabels}
    />
  );
}
