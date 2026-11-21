// /templates/$templateId (D-UI-11-04, D-UI-11-05): header (name, owning folder, published
// versions) and two tabs. Draft edits the raw document (YAML or JSON as stored) and publishes it;
// Versions lists the published versions, shows a document read-only and deprecates a version.
// A reader of published versions only gets `draft_redacted` and no Draft tab.
import { WhenBy } from "@/components/DataTable/WhenBy";
import { Eye, Upload } from "lucide-react";
import { useState } from "react";
import type { Problem } from "@/api/problem";
import type { StructuralReport, Template, TemplateVersion } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { nameRuleError, TextareaField, TextField } from "@/components/form";
import { NotFound } from "@/components/NotFound";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { Tabs } from "@/components/Tabs";
import { useCapabilities } from "@/features/access/useCapabilities";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { useFolderPathLabel } from "@/features/folders/FolderSelect";
import { isNotFound, problemOf } from "@/lib/unexpected";
import {
  useTemplate,
  useTemplateDocument,
  useTemplateVersionDocument,
  useTemplateVersions,
  useTemplateWrites,
} from "./api";
import styles from "@/features/governance/gov.module.css";
import { PrincipalName } from "@/components/PrincipalName";

export function TemplatePage({ templateId }: { templateId: string }) {
  const template = useTemplate(templateId);
  const pathOf = useFolderPathLabel();
  if (template.isError)
    return isNotFound(template.error) ? (
      <NotFound back={{ label: "templates", to: "/templates" }} />
    ) : (
      <ProblemAlert
        problem={problemOf(template.error)}
        onRetry={() => void template.refetch()}
      />
    );
  if (template.data === undefined)
    return <Skeleton label="template" rows={4} />;
  const data = template.data;
  const items = [
    ...(data.draft_redacted
      ? []
      : [
          {
            value: "draft",
            label: "Draft",
            content: <TemplateDraft template={data} />,
          },
        ]),
    {
      value: "versions",
      label: "Versions",
      content: <TemplateVersions template={data} />,
    },
  ];
  return (
    <>
      <PageHeader
        title={data.name}
        breadcrumbs={[
          { label: "Templates", to: "/templates" },
          { label: data.name },
        ]}
        badges={
          <Badge intent="neutral">
            {data.publish_counter === 1
              ? "1 published version"
              : `${String(data.publish_counter)} published versions`}
          </Badge>
        }
        description={
          <>
            Owned by <span>{pathOf(data.folder_id)}</span>
          </>
        }
      />
      <Tabs label="Template sections" items={items} />
    </>
  );
}

function StructuralSummary({ report }: { report: StructuralReport }) {
  if (report.valid)
    return (
      <Alert intent="success">
        The draft is saved and passes the ODCS schema.
      </Alert>
    );
  return (
    <Alert intent="warning" title="The draft is saved, with schema problems">
      <ul className={styles.problemList}>
        {report.errors.map((error) => (
          <li key={`${error.pointer}-${error.message}`}>
            <code className={styles.mono}>{error.pointer || "/"}</code>:{" "}
            {error.message}
          </li>
        ))}
      </ul>
    </Alert>
  );
}

function TemplateDraft({ template }: { template: Template }) {
  const caps = useCapabilities(template.folder_id);
  const documentQuery = useTemplateDocument(template.id, true);
  const { saveDraft, publish } = useTemplateWrites();
  const [text, setText] = useState<string | null>(null);
  const [name, setName] = useState(template.name);
  const [report, setReport] = useState<StructuralReport | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [published, setPublished] = useState<number | null>(null);

  const canEdit = caps.can("template:update");
  const canPublish = caps.can("template:publish");
  const nextVersion = template.publish_counter + 1;

  if (documentQuery.isError)
    return (
      <ProblemAlert
        problem={problemOf(documentQuery.error)}
        onRetry={() => void documentQuery.refetch()}
      />
    );
  if (documentQuery.data === undefined)
    return <Skeleton label="draft" rows={6} />;
  const value = text ?? documentQuery.data;
  // Only a changed name is checked: a legacy stored name never blocks saving the document.
  const nameError =
    name.trim() !== template.name ? nameRuleError(name) : undefined;
  const dirty =
    (text !== null && text !== documentQuery.data) ||
    name.trim() !== template.name;

  return (
    <div className={styles.stack}>
      {problem ? <ProblemAlert problem={problem} context="action" /> : null}
      {report ? <StructuralSummary report={report} /> : null}
      {published !== null ? (
        <Alert intent="success">
          Version {published} is published. New contracts can start from it.
        </Alert>
      ) : null}
      <div className={styles.toolbarField}>
        <TextField
          id="template-draft-name"
          label="Name"
          autoComplete="off"
          maxLength={200}
          value={name}
          error={nameError}
          readOnly={!canEdit}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </div>
      <TextareaField
        id="template-draft-document"
        label="Draft document (YAML or JSON)"
        helper={`ODCS ${template.draft_api_version ?? ""} · stored as ${template.draft_media_type ?? "unknown"}`}
        monospace
        rows={24}
        spellCheck={false}
        readOnly={!canEdit}
        value={value}
        onChange={(event) => {
          setText(event.target.value);
        }}
      />
      <div className={styles.actions}>
        {canEdit ? (
          <Button
            variant="primary"
            size="small"
            busy={saveDraft.isPending}
            busyLabel="Saving…"
            disabled={!dirty || nameError !== undefined}
            onClick={() => {
              setProblem(null);
              setPublished(null);
              saveDraft.mutate(
                {
                  templateId: template.id,
                  document: value,
                  ...(name.trim() !== template.name
                    ? { name: name.trim() }
                    : {}),
                },
                {
                  onSuccess: (result) => {
                    setReport(result.structural_validation);
                    setText(null);
                  },
                  onError: (error) => {
                    setProblem(problemOf(error));
                  },
                },
              );
            }}
          >
            Save draft
          </Button>
        ) : null}
        {canPublish ? (
          <Button
            variant="secondary"
            size="small"
            icon={Upload}
            disabled={dirty}
            onClick={() => {
              setConfirming(true);
            }}
          >
            Publish template
          </Button>
        ) : null}
        {dirty && canPublish ? (
          <span className={styles.muted}>
            Save the draft before publishing.
          </span>
        ) : null}
      </div>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Publish template version ${String(nextVersion)}?`}
        body="Contracts created from it start from this content. Existing contracts are not changed."
        dismissLabel="Go back"
        confirmLabel="Publish template"
        busyLabel="Publishing…"
        onConfirm={async () => {
          const result = await publish.mutateAsync(template.id);
          setPublished(result.version.version_no);
          setReport(null);
        }}
      />
    </div>
  );
}

function TemplateVersions({ template }: { template: Template }) {
  const caps = useCapabilities(template.folder_id);
  const versions = useTemplateVersions(template.id);
  const { deprecate } = useTemplateWrites();
  const [viewing, setViewing] = useState<number | null>(null);
  const [deprecating, setDeprecating] = useState<TemplateVersion | null>(null);
  const document = useTemplateVersionDocument(template.id, viewing);
  const canDeprecate = caps.can("template:deprecate");

  const columns: Column<TemplateVersion>[] = [
    {
      id: "number",
      header: "Version",
      rowHeader: true,
      sortValue: (row) => row.version_no,
      cell: (row) => `Version ${String(row.version_no)}`,
    },
    { id: "name", header: "Name", cell: (row) => row.name },
    {
      id: "odcs",
      header: "ODCS",
      cell: (row) => (
        <span className={styles.ident}>{row.odcs_api_version}</span>
      ),
    },
    {
      id: "published",
      header: "Published",
      cell: (row) => (
        <WhenBy
          when={formatDateTime(row.created_at)}
          by={<PrincipalName id={row.created_by} />}
        />
      ),
    },
    {
      id: "state",
      header: "State",
      cell: (row) =>
        row.deprecated ? (
          <Badge intent="warning">Deprecated</Badge>
        ) : (
          <Badge intent="success">Offered</Badge>
        ),
    },
    {
      id: "actions",
      header: "Actions",
      cell: (row) => (
        <span className={styles.actions}>
          <Button
            variant="ghost"
            size="small"
            icon={Eye}
            onClick={() => {
              setViewing(row.version_no);
            }}
          >
            View version {row.version_no}
          </Button>
          {canDeprecate && !row.deprecated ? (
            <Button
              variant="ghost"
              size="small"
              onClick={() => {
                setDeprecating(row);
              }}
            >
              Deprecate version {row.version_no}
            </Button>
          ) : null}
        </span>
      ),
    },
  ];

  if (versions.isError)
    return (
      <ProblemAlert
        problem={problemOf(versions.error)}
        onRetry={() => void versions.refetch()}
      />
    );
  if (versions.data === undefined)
    return <Skeleton label="versions" rows={3} />;
  return (
    <>
      {versions.data.length === 0 ? (
        <EmptyState
          level={2}
          heading="No published versions"
          body="Publish the draft to offer this template to this folder and the folders below it."
        />
      ) : (
        <DataTable
          caption={`Versions of ${template.name}`}
          captionHidden
          columns={columns}
          rows={[...versions.data].reverse()}
          getRowId={(row) => row.id}
        />
      )}
      <Dialog
        open={viewing !== null}
        onOpenChange={(open) => {
          if (!open) setViewing(null);
        }}
        size={896}
        title={`Version ${String(viewing ?? "")} of ${template.name}`}
        description="Read-only. Published versions never change."
      >
        {document.isError ? (
          <ProblemAlert problem={problemOf(document.error)} />
        ) : document.data === undefined ? (
          <Skeleton label="document" rows={6} />
        ) : (
          <pre className={styles.code}>{document.data}</pre>
        )}
      </Dialog>
      <ConfirmDialog
        open={deprecating !== null}
        onOpenChange={(open) => {
          if (!open) setDeprecating(null);
        }}
        title={`Deprecate template version ${String(deprecating?.version_no ?? "")}?`}
        body="It stays readable but is no longer offered for new contracts."
        objectName={`version ${String(deprecating?.version_no ?? "")}`}
        confirmLabel="Deprecate version"
        busyLabel="Deprecating…"
        onConfirm={async () => {
          if (deprecating === null) return;
          await deprecate.mutateAsync({
            templateId: template.id,
            versionNo: deprecating.version_no,
          });
        }}
      />
    </>
  );
}
