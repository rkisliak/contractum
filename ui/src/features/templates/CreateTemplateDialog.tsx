// Create a template at a folder (D-UI-11-04): name, owning folder and the first draft document.
// The API needs a document to create the draft, so the field starts from a small ODCS skeleton
// the author edits. On success the template page opens.
import { useNavigate } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import { Button } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { nameRuleError, TextareaField, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { FolderSelect } from "@/features/folders/FolderSelect";
import { problemOf } from "@/lib/unexpected";
import { STARTER_TEMPLATE, useTemplateWrites } from "./api";
import styles from "@/features/governance/gov.module.css";

export function CreateTemplateDialog({
  open,
  onOpenChange,
  folderId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  folderId?: string;
}) {
  const navigate = useNavigate();
  const { create } = useTemplateWrites();
  const [name, setName] = useState("");
  const [folder, setFolder] = useState(folderId ?? "");
  const [document, setDocument] = useState(STARTER_TEMPLATE);
  const [errors, setErrors] = useState<{ name?: string; folder?: string }>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const formId = "create-template-form";

  function reset() {
    setName("");
    setFolder(folderId ?? "");
    setDocument(STARTER_TEMPLATE);
    setErrors({});
    setProblem(null);
    create.reset();
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const found: { name?: string; folder?: string } = {};
    if (name.trim() === "") found.name = "Enter a name for the template.";
    else found.name = nameRuleError(name);
    if (folder === "") found.folder = "Choose the folder that owns it.";
    setErrors(found);
    setProblem(null);
    if (found.name || found.folder) return;
    create.mutate(
      { folderId: folder, name: name.trim(), document },
      {
        onSuccess: (result) => {
          reset();
          onOpenChange(false);
          void navigate({
            to: "/templates/$templateId",
            params: { templateId: result.template.id },
          });
        },
        onError: (error) => {
          setProblem(problemOf(error));
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      size={896}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Create template"
      description="This folder and the folders below it can start new contracts from its published versions."
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
            Create template
          </Button>
        </>
      }
    >
      <form id={formId} className={styles.form} noValidate onSubmit={onSubmit}>
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <TextField
          id="template-name"
          label="Name"
          autoComplete="off"
          maxLength={200}
          value={name}
          error={errors.name ?? nameRuleError(name)}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
        <FolderSelect
          id="template-folder"
          label="Owning folder"
          controlSize="default"
          placeholder="Choose a folder"
          value={folder}
          onChange={setFolder}
        />
        {errors.folder ? (
          <p className={styles.bad} role="alert">
            {errors.folder}
          </p>
        ) : null}
        <TextareaField
          id="template-document"
          label="First draft (YAML or JSON)"
          helper="An ODCS 3.1 or 3.2 document. You can edit it after creating the template."
          monospace
          rows={14}
          spellCheck={false}
          value={document}
          onChange={(event) => {
            setDocument(event.target.value);
          }}
        />
      </form>
    </Dialog>
  );
}
