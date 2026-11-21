// The folder "Folders" tab: the direct children (archived ones carry a badge). The create action is
// the folder header's own button, so the empty state offers none (one New per screen).
import { Link } from "@tanstack/react-router";
import { FolderPlus } from "lucide-react";
import type { Folder } from "@/api/types";
import { Badge } from "@/components/Badge";
import { DataTable, type Column } from "@/components/DataTable";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { childKindOf, kindLabel } from "@/features/access/roles";
import { problemOf } from "@/lib/unexpected";
import { useFolderList } from "./api";
import { folderName } from "./tree";
import { formatDateTime } from "./FolderOverview";
import styles from "./FolderPages.module.css";

export function FolderChildren({ folder }: { folder: Folder }) {
  const list = useFolderList(true);
  const childKind = childKindOf(folder.kind);

  if (list.isError)
    return (
      <ProblemAlert
        problem={problemOf(list.error)}
        onRetry={() => void list.refetch()}
      />
    );
  if (list.data === undefined) return <Skeleton label="folders" rows={3} />;

  const children = list.data.folders
    .filter((candidate) => candidate.parent_id === folder.id)
    .sort((a, b) => a.display_name.localeCompare(b.display_name));

  const columns: Column<Folder>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      sortValue: (row) => row.display_name,
      cell: (row) => (
        <Link
          to="/folders/$folderId"
          params={{ folderId: row.id }}
          className={styles.rowLink}
        >
          {row.display_name}
        </Link>
      ),
    },
    { id: "kind", header: "Kind", cell: (row) => kindLabel(row.kind) },
    {
      id: "path",
      header: "Path",
      cell: (row) => <span className={styles.ident}>{row.path}</span>,
    },
    {
      id: "status",
      header: "Status",
      cell: (row) =>
        row.archived ? (
          <Badge intent="warning">Archived</Badge>
        ) : (
          <Badge intent="success">Active</Badge>
        ),
    },
    {
      id: "updated",
      header: "Updated",
      sortValue: (row) => row.updated_at,
      cell: (row) => formatDateTime(row.updated_at),
    },
  ];

  return (
    <section className={styles.stack} aria-label="Folders">
      {children.length === 0 ? (
        <EmptyState
          icon={FolderPlus}
          heading="No folders yet"
          body={
            childKind
              ? `${kindLabel(childKind)} folders created inside ${folderName(folder)} appear here.`
              : "Team folders have no folders inside them."
          }
        />
      ) : (
        <DataTable
          caption={`Folders in ${folderName(folder)}`}
          columns={columns}
          rows={children}
          getRowId={(row) => row.id}
        />
      )}
    </section>
  );
}
