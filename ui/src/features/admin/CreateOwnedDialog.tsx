// The "Create local group" and "Create service account" dialogs of the merged admin lists (quick
// 261121-0sh): both ask for a name and an owner folder, check the name rule before sending, map
// the API's field errors and a 409 onto the form, then settle and open the new record. One
// component keeps the two forms identical; each page supplies its own copy, ids and create call.
import { useMutation } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import type { Problem } from "@/api/problem";
import { Button } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { nameRuleError, SelectField, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { problemOf } from "@/lib/unexpected";
import styles from "./admin.module.css";

type CreateBody = { name: string; owner_folder_id: string };
type FieldErrors = { name?: string; owner?: string };

export function CreateOwnedDialog({
  open,
  onOpenChange,
  owners,
  idPrefix,
  title,
  description,
  emptyNameMessage,
  conflictMessage,
  create,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  owners: { value: string; label: string }[];
  /** Prefix of the field and form ids, for example "group" or "sa". */
  idPrefix: string;
  /** The dialog title, also the label of the submit button. */
  title: string;
  description: string;
  /** Shown when the name is blank, for example "Enter a group name.". */
  emptyNameMessage: string;
  /** Shown on the name field after a 409, for example "A group with this name already exists here.". */
  conflictMessage: string;
  /** The POST: resolves with the new record's id, rejects with the API error. */
  create: (body: CreateBody) => Promise<{ id: string }>;
  /** Called once after the dialog has been reset and closed: settle the list, open the record. */
  onCreated: (id: string) => void;
}) {
  const [name, setName] = useState("");
  const [owner, setOwner] = useState("");
  const [errors, setErrors] = useState<FieldErrors>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const mutation = useMutation({ mutationFn: create });
  const ownerValue = owner || owners[0]?.value || "";
  const formId = `create-${idPrefix}-form`;

  function reset() {
    setName("");
    setOwner("");
    setErrors({});
    setProblem(null);
    mutation.reset();
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const found: FieldErrors = {};
    if (name.trim() === "") found.name = emptyNameMessage;
    else found.name = nameRuleError(name);
    if (ownerValue === "") found.owner = "Choose an owner.";
    setErrors(found);
    setProblem(null);
    if (found.name || found.owner) return;
    mutation.mutate(
      { name: name.trim(), owner_folder_id: ownerValue },
      {
        onSuccess: (record) => {
          reset();
          onOpenChange(false);
          onCreated(record.id);
        },
        onError: (error) => {
          const failure = problemOf(error);
          const mapped = mapFieldErrors(failure, {
            "/name": "name",
            "/owner_folder_id": "owner",
          });
          setErrors(mapped.fieldErrors);
          if (failure.kind === "problem" && failure.status === 409) {
            setErrors({ name: conflictMessage });
            return;
          }
          if (Object.keys(mapped.fieldErrors).length === 0) setProblem(failure);
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
      title={title}
      description={description}
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
            busy={mutation.isPending}
            busyLabel="Creating…"
          >
            {title}
          </Button>
        </>
      }
    >
      <form id={formId} className={styles.form} noValidate onSubmit={onSubmit}>
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <TextField
          id={`${idPrefix}-name`}
          label="Name"
          autoComplete="off"
          value={name}
          error={errors.name ?? nameRuleError(name)}
          maxLength={200}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
        <SelectField
          id={`${idPrefix}-owner`}
          label="Owner"
          value={ownerValue}
          options={owners}
          error={errors.owner}
          onChange={(event) => {
            setOwner(event.target.value);
          }}
        />
      </form>
    </Dialog>
  );
}
