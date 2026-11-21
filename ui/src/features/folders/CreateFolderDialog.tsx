// Create a child folder (D-UI-09-04). The kind follows from the parent and is shown, not chosen.
// The slug is suggested from the name until the user edits it; it is immutable after creation.
import { useNavigate } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import type { Problem } from "@/api/problem";
import { Button } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { nameRuleError, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { kindLabel } from "@/features/access/roles";
import { problemOf } from "@/lib/unexpected";
import { useFolderWrites } from "./api";
import { folderCopy } from "./copy";
import { isValidSlug, suggestSlug } from "./slug";
import styles from "./FolderDialogs.module.css";

export function CreateFolderDialog({
  open,
  onOpenChange,
  parentId,
  parentName,
  childKind,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  parentId: string;
  parentName: string;
  childKind: string;
}) {
  const navigate = useNavigate();
  const { create } = useFolderWrites();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [errors, setErrors] = useState<{ name?: string; slug?: string }>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const formId = "create-folder-form";
  const kind = kindLabel(childKind).toLowerCase();

  function reset() {
    setName("");
    setSlug("");
    setSlugEdited(false);
    setErrors({});
    setProblem(null);
    create.reset();
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const found: { name?: string; slug?: string } = {};
    if (name.trim() === "") found.name = `Enter a name for the ${kind}.`;
    else found.name = nameRuleError(name);
    if (!isValidSlug(slug))
      found.slug = "Use lowercase letters, digits and single hyphens.";
    setErrors(found);
    setProblem(null);
    if (found.name || found.slug) return;
    create.mutate(
      { parentId, slug, displayName: name.trim() },
      {
        onSuccess: (folder) => {
          reset();
          onOpenChange(false);
          void navigate({
            to: "/folders/$folderId",
            params: { folderId: folder.id },
          });
        },
        onError: (error) => {
          const found = problemOf(error);
          const mapped = mapFieldErrors(found, {
            "/display_name": "name",
            "/slug": "slug",
          });
          setErrors(mapped.fieldErrors);
          if (
            found.kind === "problem" &&
            found.status === 409 &&
            found.slug === "conflict"
          ) {
            setErrors({ slug: "A folder with this slug already exists here." });
            return;
          }
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
      title={folderCopy.createLabel(childKind)}
      description={`The new ${kind} is created inside ${parentName}.`}
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
            {folderCopy.createLabel(childKind)}
          </Button>
        </>
      }
    >
      <form id={formId} className={styles.form} noValidate onSubmit={onSubmit}>
        {problem ? (
          <ProblemAlert
            problem={problem}
            context="action"
            fieldLabels={{ "/display_name": "Name", "/slug": "Slug" }}
          />
        ) : null}
        <TextField
          id="folder-name"
          label="Name"
          autoComplete="off"
          value={name}
          error={errors.name ?? nameRuleError(name)}
          maxLength={200}
          onChange={(event) => {
            setName(event.target.value);
            if (!slugEdited) setSlug(suggestSlug(event.target.value));
          }}
        />
        <TextField
          id="folder-slug"
          label="Slug"
          autoComplete="off"
          helper={folderCopy.slugHelper}
          value={slug}
          error={errors.slug}
          maxLength={63}
          spellCheck={false}
          onChange={(event) => {
            setSlugEdited(true);
            setSlug(event.target.value);
          }}
        />
        <p className={styles.kind}>
          Kind: <strong>{kindLabel(childKind)}</strong>
        </p>
      </form>
    </Dialog>
  );
}
