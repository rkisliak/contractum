// Rename a folder (D-UI-09-04): the display name only; the slug and path never change.
import { useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import type { Problem } from "@/api/problem";
import type { Folder } from "@/api/types";
import { Button } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { nameRuleError, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { problemOf } from "@/lib/unexpected";
import { useFolderWrites } from "./api";
import styles from "./FolderDialogs.module.css";

export function RenameFolderDialog({
  open,
  onOpenChange,
  folder,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  folder: Folder;
}) {
  const { rename } = useFolderWrites();
  const [name, setName] = useState(folder.display_name);
  const [error, setError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);
  const formId = "rename-folder-form";

  function close() {
    setName(folder.display_name);
    setError(undefined);
    setProblem(null);
    rename.reset();
    onOpenChange(false);
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    if (name.trim() === "") {
      setError("Enter a name for the folder.");
      return;
    }
    const refused = nameRuleError(name);
    setError(refused);
    if (refused) return;
    rename.mutate(
      { folderId: folder.id, displayName: name.trim() },
      {
        onSuccess: () => {
          onOpenChange(false);
        },
        onError: (failure) => {
          const found = problemOf(failure);
          const mapped = mapFieldErrors(found, { "/display_name": "name" });
          setError(mapped.fieldErrors.name);
          if (mapped.fieldErrors.name === undefined) setProblem(found);
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close();
        else onOpenChange(true);
      }}
      title={`Rename ${folder.display_name}`}
      description="Only the display name changes. The slug and the folder path stay the same."
      footer={
        <>
          <Button variant="secondary" size="small" onClick={close}>
            Keep current name
          </Button>
          <Button
            variant="primary"
            size="small"
            type="submit"
            form={formId}
            busy={rename.isPending}
            busyLabel="Saving…"
          >
            Rename folder
          </Button>
        </>
      }
    >
      <form id={formId} className={styles.form} noValidate onSubmit={onSubmit}>
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <TextField
          id="rename-folder-name"
          label="Name"
          autoComplete="off"
          value={name}
          error={error ?? nameRuleError(name)}
          maxLength={200}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </form>
    </Dialog>
  );
}
