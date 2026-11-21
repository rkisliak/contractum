// The templates table shared by /templates and the folder Templates tab ("Defined here").
import { Link } from "@tanstack/react-router";
import { LayoutTemplate } from "lucide-react";
import type { Template } from "@/api/types";
import { Badge } from "@/components/Badge";
import { DataTable, type Column } from "@/components/DataTable";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { useCursorList } from "@/lib/useCursorList";
import { OwningFolder } from "@/features/folders/OwningFolder";
import { problemOf } from "@/lib/unexpected";
import { templateKeys, useTemplatePage } from "./api";
import styles from "@/features/governance/gov.module.css";

export function TemplateTable({
  folderId,
  caption,
  emptyBody,
}: {
  folderId: string | null;
  caption: string;
  emptyBody: string;
}) {
  const fetchPage = useTemplatePage();
  const list = useCursorList<Template>({
    queryKey: templateKeys.list(folderId),
    fetchPage: (cursor) => fetchPage(folderId, cursor),
  });

  const columns: Column<Template>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      sortValue: (row) => row.name,
      cell: (row) => (
        <Link
          to="/templates/$templateId"
          params={{ templateId: row.id }}
          className={styles.rowLink}
        >
          {row.name}
        </Link>
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
      cell: (row) =>
        row.latest_version_no === null ? (
          <Badge intent="neutral">Draft only</Badge>
        ) : (
          `Version ${String(row.latest_version_no)}`
        ),
    },
    {
      id: "published",
      header: "Published versions",
      align: "end",
      sortValue: (row) => row.publish_counter,
      cell: (row) => row.publish_counter,
    },
    {
      id: "updated",
      header: "Updated",
      sortValue: (row) => row.updated_at,
      cell: (row) => formatDateTime(row.updated_at),
    },
  ];

  if (list.error)
    return (
      <ProblemAlert problem={problemOf(list.error)} onRetry={list.refetch} />
    );
  if (list.isPending) return <Skeleton label="templates" rows={3} />;
  if (list.rows.length === 0)
    return (
      <EmptyState
        level={2}
        icon={LayoutTemplate}
        heading="No templates yet"
        body={emptyBody}
      />
    );
  return (
    <DataTable
      caption={caption}
      columns={columns}
      rows={list.rows}
      getRowId={(row) => row.id}
      loadMore={{
        hasMore: list.hasMore,
        isLoadingMore: list.isLoadingMore,
        onLoadMore: list.loadMore,
        error: list.pageError,
      }}
    />
  );
}
