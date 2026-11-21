// /templates: every template the caller can see, optionally narrowed to one owning folder
// (`?folder_id=`), with "Create template" for callers who may create templates.
import { Plus } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/Button";
import { PageHeader } from "@/components/PageHeader";
import { useCapabilities } from "@/features/access/useCapabilities";
import { FolderSelect, useRootFolder } from "@/features/folders/FolderSelect";
import { CreateTemplateDialog } from "./CreateTemplateDialog";
import { TemplateTable } from "./TemplateTable";
import styles from "@/features/governance/gov.module.css";

export function TemplatesPage({
  folderId,
  onFolderChange,
}: {
  folderId: string | undefined;
  onFolderChange: (folderId: string | undefined) => void;
}) {
  const root = useRootFolder();
  const caps = useCapabilities(folderId ?? root?.id ?? null);
  const [creating, setCreating] = useState(false);
  const canCreate = caps.can("template:create");
  const create = canCreate ? (
    <Button
      variant="primary"
      size="small"
      icon={Plus}
      onClick={() => {
        setCreating(true);
      }}
    >
      Create template
    </Button>
  ) : undefined;

  return (
    <>
      <PageHeader
        title="Templates"
        description="Reusable starting points for contracts. A template published at a folder is offered to every team below it."
        breadcrumbs={[{ label: "Home", to: "/" }, { label: "Templates" }]}
        actions={create}
      />
      <div className={styles.stack}>
        <div className={styles.toolbar}>
          <div className={styles.toolbarField}>
            <FolderSelect
              id="templates-folder"
              label="Owning folder"
              placeholder="Every folder you can see"
              value={folderId ?? ""}
              onChange={(value) => {
                onFolderChange(value === "" ? undefined : value);
              }}
            />
          </div>
        </div>
        <TemplateTable
          folderId={folderId ?? null}
          caption="Templates"
          emptyBody={
            folderId
              ? "No template is owned by this folder. Templates from folders above it are listed on the folder's Templates tab."
              : "Create a template at an organization, project or team so new contracts start from it."
          }
        />
      </div>
      <CreateTemplateDialog
        open={creating}
        onOpenChange={setCreating}
        folderId={folderId}
      />
    </>
  );
}
