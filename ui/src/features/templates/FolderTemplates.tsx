// The folder Templates tab (D-UI-11-03): "Defined here" (templates this folder owns) and
// "Available here" (published, non-deprecated templates from this folder and every folder above).
// "Defined here" needs template:read at the folder and is left out without it. A template owned
// by a folder the caller cannot see is named without a link: its page would answer Not found.
// "Available here" needs contract:create (the API refuses it otherwise).
import { Link } from "@tanstack/react-router";
import { Plus } from "lucide-react";
import { useState } from "react";
import type { AvailableTemplate, Folder } from "@/api/types";
import { Button } from "@/components/Button";
import { DataTable, type Column } from "@/components/DataTable";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, NotAvailable, Skeleton } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { useFolderList } from "@/features/folders/api";
import { useCursorList } from "@/lib/useCursorList";
import { OwningFolder } from "@/features/folders/OwningFolder";
import { problemOf } from "@/lib/unexpected";
import { templateKeys, useAvailableTemplatePage } from "./api";
import { CreateTemplateDialog } from "./CreateTemplateDialog";
import { TemplateTable } from "./TemplateTable";
import styles from "@/features/governance/gov.module.css";

export function FolderTemplates({ folder }: { folder: Folder }) {
  const caps = useCapabilities(folder.id);
  const [creating, setCreating] = useState(false);
  const canCreate = caps.can("template:create") && !folder.archived;
  const canRead = caps.can("template:read");
  const canStartContracts = caps.can("contract:create");
  if (!caps.isPending && !canRead && !canStartContracts)
    return <NotAvailable what="Templates at this folder" />;
  return (
    <div className={styles.stack}>
      {canRead ? (
        <section className={styles.section} aria-labelledby="templates-here">
          <div className={styles.sectionHeader}>
            <h2 id="templates-here" className={styles.sectionHeading}>
              Defined here
            </h2>
            {canCreate ? (
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
            ) : null}
          </div>
          <TemplateTable
            folderId={folder.id}
            caption={`Templates owned by ${folder.display_name}`}
            emptyBody="This folder owns no template. Templates created here are offered to this folder and every folder below it."
          />
        </section>
      ) : null}
      <section className={styles.section} aria-labelledby="templates-available">
        <h2 id="templates-available" className={styles.sectionHeading}>
          Available here
        </h2>
        {caps.isPending ? (
          <Skeleton label="available templates" rows={2} />
        ) : canStartContracts ? (
          <AvailableTemplates folder={folder} />
        ) : (
          <NotAvailable level={3} what="Starting contracts from templates" />
        )}
      </section>
      <CreateTemplateDialog
        open={creating}
        onOpenChange={setCreating}
        folderId={folder.id}
      />
    </div>
  );
}

function AvailableTemplates({ folder }: { folder: Folder }) {
  const caps = useCapabilities(folder.id);
  // Contracts live in folders of any kind; the API re-checks the permission.
  const canStart = !folder.archived && caps.can("contract:create");
  const fetchPage = useAvailableTemplatePage();
  const visibleFolders = new Set(
    (useFolderList().data?.folders ?? []).map((f) => f.id),
  );
  const list = useCursorList<AvailableTemplate>({
    queryKey: templateKeys.available(folder.id),
    fetchPage: (cursor) => fetchPage(folder.id, cursor),
  });
  const columns: Column<AvailableTemplate>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      sortValue: (row) => row.name,
      cell: (row) =>
        visibleFolders.has(row.folder_id) ? (
          <Link
            to="/templates/$templateId"
            params={{ templateId: row.template_id }}
            className={styles.rowLink}
          >
            {row.name}
          </Link>
        ) : (
          row.name
        ),
    },
    {
      id: "folder",
      header: "Owning folder",
      cell: (row) => <OwningFolder folderId={row.folder_id} />,
    },
    {
      id: "latest",
      header: "Latest version",
      cell: (row) => `Version ${String(row.latest_version_no)}`,
    },
    {
      id: "offered",
      header: "Offered versions",
      cell: (row) => row.version_nos.join(", "),
    },
    ...(canStart
      ? [
          {
            id: "use",
            header: "Actions",
            align: "end" as const,
            cell: (row: AvailableTemplate) => (
              <Link
                to="/contracts/new"
                search={{ folder: folder.id, template: row.template_id }}
                className={styles.rowLink}
                aria-label={`Use in new contract: ${row.name}`}
              >
                Use in new contract
              </Link>
            ),
          },
        ]
      : []),
  ];
  if (list.error)
    return (
      <ProblemAlert problem={problemOf(list.error)} onRetry={list.refetch} />
    );
  if (list.isPending) return <Skeleton label="available templates" rows={2} />;
  if (list.rows.length === 0)
    return (
      <EmptyState
        level={3}
        heading="No templates available"
        body="No published template applies here yet."
      />
    );
  return (
    <DataTable
      caption={`Templates available in ${folder.display_name}`}
      captionHidden
      columns={columns}
      rows={list.rows}
      getRowId={(row) => row.template_id}
      loadMore={{
        hasMore: list.hasMore,
        isLoadingMore: list.isLoadingMore,
        onLoadMore: list.loadMore,
        error: list.pageError,
      }}
    />
  );
}
