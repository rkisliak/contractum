// Create a rule set with an empty draft at a folder, then open its page on the Draft tab.
import { useNavigate } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import { mapFieldErrors } from "@/api/fieldErrors";
import { Button } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { nameRuleError, TextareaField, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { problemOf } from "@/lib/unexpected";
import { useCreateRuleSet } from "./api";
import styles from "@/features/governance/gov.module.css";

export function CreateRuleSetDialog({
  open,
  onOpenChange,
  folderId,
  folderName,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  folderId: string;
  folderName: string;
}) {
  const navigate = useNavigate();
  const create = useCreateRuleSet();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);
  const formId = "create-rule-set-form";

  function reset() {
    setName("");
    setDescription("");
    setError(undefined);
    setProblem(null);
    create.reset();
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    if (name.trim() === "") {
      setError("Enter a name for the rule set.");
      return;
    }
    const refused = nameRuleError(name);
    setError(refused);
    if (refused) return;
    create.mutate(
      {
        folderId,
        name: name.trim(),
        description: description.trim() === "" ? null : description.trim(),
      },
      {
        onSuccess: (ruleSet) => {
          reset();
          onOpenChange(false);
          void navigate({
            to: "/rule-sets/$ruleSetId",
            params: { ruleSetId: ruleSet.id },
          });
        },
        onError: (failure) => {
          const found = problemOf(failure);
          if (found.status === 409) {
            setError("A rule set with this name already exists here.");
            return;
          }
          const mapped = mapFieldErrors(found, { "/name": "name" });
          setError(mapped.fieldErrors.name);
          setProblem(found);
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Create rule set"
      description={`The rule set belongs to ${folderName}. Its rules can govern contracts there and below once published.`}
      footer={
        <>
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              reset();
              onOpenChange(false);
            }}
          >
            Go back
          </Button>
          <Button
            variant="primary"
            size="small"
            type="submit"
            form={formId}
            busy={create.isPending}
            busyLabel="Creating…"
          >
            Create rule set
          </Button>
        </>
      }
    >
      <form id={formId} className={styles.form} noValidate onSubmit={onSubmit}>
        {problem ? (
          <ProblemAlert
            problem={problem}
            context="action"
            fieldLabels={{ "/name": "Name", "/description": "Description" }}
          />
        ) : null}
        <TextField
          id="rule-set-name"
          label="Name"
          autoComplete="off"
          maxLength={200}
          placeholder="acme-baseline"
          value={name}
          error={error ?? nameRuleError(name)}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
        <TextareaField
          id="rule-set-description"
          label="Description"
          optional
          rows={3}
          maxLength={2000}
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
      </form>
    </Dialog>
  );
}
