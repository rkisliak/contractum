// New contract (D-UI-09-08): pick a folder of any kind (Root, organization, project or team), then one of five sources: blank YAML/JSON,
// the ODCS starter, a published template available at the folder, a version or draft of
// another contract, or an uploaded file (read in the browser, size checked against 2 MiB, POSTed
// raw). On success the page opens the new contract's draft editor. Problems render here,
// including an unsupported apiVersion and a duplicate ODCS id.
import { useCapabilities } from "@/features/access/useCapabilities";
import { holdsAny, useHeldAccess } from "@/features/access/useHeldAccess";
import { NotAvailable } from "@/components/states";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import {
  Copy,
  FilePlus2,
  FileUp,
  LayoutTemplate,
  Sparkles,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState, type ReactNode } from "react";
import type { Problem } from "@/api/problem";
import type { ContractCreated, Folder } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { nameRuleError, SelectField, TextField } from "@/components/form";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { CodeEditor } from "@/features/editor/CodeEditor";
import { useFolderList } from "@/features/folders/api";
import { usePublicSettings } from "@/features/settings/api";
import { fetchAllPages } from "@/lib/fetchAllPages";
import { problemOf } from "@/lib/unexpected";
import { toSlug } from "@/lib/text";
import {
  contractKeys,
  useApi,
  useContractVersions,
  useDrafts,
  type DocFormat,
} from "./api";
import { ODCS_ID_MAX_LENGTH } from "./limits";
import styles from "./Contracts.module.css";

export type NewContractSearch = {
  folder?: string;
  /** Preselects "From a template" with this template (the template page's "Use in new contract"). */
  template?: string;
};

type Source = "blank" | "starter" | "template" | "version" | "upload";

const SOURCES: {
  value: Source;
  label: string;
  help: string;
  icon: LucideIcon;
}[] = [
  {
    value: "starter",
    label: "ODCS starter",
    help: "A minimal valid ODCS document.",
    icon: Sparkles,
  },
  {
    value: "blank",
    label: "Blank document",
    help: "Write or paste YAML or JSON.",
    icon: FilePlus2,
  },
  {
    value: "template",
    label: "From a template",
    help: "A published template available here.",
    icon: LayoutTemplate,
  },
  {
    value: "version",
    label: "From another contract",
    help: "Copy a version or draft you can read.",
    icon: Copy,
  },
  {
    value: "upload",
    label: "Upload a file",
    help: ".yaml, .yml or .json, up to 2 MiB.",
    icon: FileUp,
  },
];

const CANNOT_CREATE = "You cannot create contracts in this folder.";

export const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;

function slugify(value: string): string {
  return toSlug(value);
}

export function suggestOdcsId(
  folder: Folder | undefined,
  name: string,
): string {
  const parts = [
    ...(folder?.path.split("/").filter(Boolean) ?? []),
    slugify(name) || "contract",
  ];
  return `urn:${parts.join(":")}`;
}

/** The ODCS version a blank document starts with when the platform default cannot be read. */
export const FALLBACK_API_VERSION = "v3.2.0";

export function blankSkeleton(
  format: DocFormat,
  odcsId: string,
  name: string,
  apiVersion: string = FALLBACK_API_VERSION,
): string {
  if (format === "json")
    return `${JSON.stringify(
      {
        apiVersion,
        kind: "DataContract",
        id: odcsId,
        name,
        version: "0.1.0",
        status: "draft",
      },
      null,
      2,
    )}\n`;
  return `apiVersion: ${apiVersion}\nkind: DataContract\nid: ${odcsId}\nname: ${name}\nversion: "0.1.0"\nstatus: draft\n`;
}

function SourcePicker({
  value,
  onChange,
}: {
  value: Source;
  onChange: (next: Source) => void;
}) {
  return (
    <fieldset className={styles.sources}>
      <legend>Start from</legend>
      {SOURCES.map((source) => {
        const Icon = source.icon;
        return (
          <label key={source.value} className={styles.sourceOption}>
            <span className={styles.sourceTitle}>
              <input
                type="radio"
                name="contract-source"
                value={source.value}
                checked={value === source.value}
                onChange={() => {
                  onChange(source.value);
                }}
              />
              <Icon size={16} aria-hidden="true" />
              {source.label}
            </span>
            <span className={styles.muted}>{source.help}</span>
          </label>
        );
      })}
    </fieldset>
  );
}

function TemplateFields({
  folderId,
  templateId,
  setTemplateId,
  templateVersion,
  setTemplateVersion,
}: {
  folderId: string;
  templateId: string;
  setTemplateId: (value: string) => void;
  templateVersion: string;
  setTemplateVersion: (value: string) => void;
}) {
  const api = useApi();
  const templates = useQuery({
    queryKey: ["contracts", "available-templates", folderId],
    // Every available template, not just the first page (the select has no server search).
    queryFn: async () =>
      (
        await fetchAllPages(async (query) => {
          const { data } = await api.GET(
            "/api/v1/contracts/folders/{folder_id}/available-templates",
            { params: { path: { folder_id: folderId }, query } },
          );
          return data;
        })
      ).items,
  });
  const selected = templates.data?.find(
    (item) => item.template_id === templateId,
  );
  const versionNo = templateVersion
    ? Number(templateVersion)
    : selected?.latest_version_no;
  const preview = useQuery({
    queryKey: [
      "contracts",
      "template-preview",
      folderId,
      templateId,
      versionNo,
    ],
    enabled: Boolean(selected && versionNo),
    queryFn: async () => {
      const { data } = await api.GET(
        "/api/v1/contracts/folders/{folder_id}/available-templates/{template_id}/versions/{version_no}/document",
        {
          params: {
            path: {
              folder_id: folderId,
              template_id: templateId,
              version_no: versionNo ?? 1,
            },
          },
          parseAs: "text",
        },
      );
      return typeof data === "string" ? data : "";
    },
  });
  if (templates.error)
    return <ProblemAlert problem={problemOf(templates.error)} />;
  if (templates.data && templates.data.length === 0)
    return (
      <Alert intent="info">
        No published templates are available at this folder.
      </Alert>
    );
  return (
    <>
      <div className={styles.formRow}>
        <SelectField
          id="new-template"
          label="Template"
          placeholder="Choose a template"
          options={(templates.data ?? []).map((item) => ({
            value: item.template_id,
            label: `${item.name} (latest v${String(item.latest_version_no)})`,
          }))}
          value={templateId}
          onChange={(event) => {
            setTemplateId(event.target.value);
            setTemplateVersion("");
          }}
        />
        <SelectField
          id="new-template-version"
          label="Template version"
          placeholder="Latest"
          disabled={!selected}
          options={(selected?.version_nos ?? []).map((no) => ({
            value: String(no),
            label: `Version ${String(no)}`,
          }))}
          value={templateVersion}
          onChange={(event) => {
            setTemplateVersion(event.target.value);
          }}
        />
      </div>
      {preview.data !== undefined ? (
        <CodeEditor
          value={preview.data}
          language="yaml"
          readOnly
          label="Template preview"
          fit="tall"
        />
      ) : null}
    </>
  );
}

function VersionFields({
  sourceId,
  setSourceId,
  sourceRef,
  setSourceRef,
}: {
  sourceId: string;
  setSourceId: (value: string) => void;
  sourceRef: string;
  setSourceRef: (value: string) => void;
}) {
  const api = useApi();
  const [q, setQ] = useState("");
  const contracts = useQuery({
    queryKey: ["contracts", "picker", q],
    queryFn: async () => {
      const { data } = await api.GET("/api/v1/contracts/contracts", {
        params: { query: { limit: 100, ...(q ? { q } : {}) } },
      });
      return data?.items ?? [];
    },
  });
  const versions = useContractVersions(sourceId, sourceId !== "");
  const drafts = useDrafts(sourceId, sourceId !== "");
  const refOptions = [
    ...(versions.data ?? []).map((version) => ({
      value: `v:${version.semver}`,
      label: `Version ${version.semver}${version.lifecycle === "unpublished" ? " (unpublished)" : ""}`,
    })),
    ...(drafts.data ?? []).map((draft) => ({
      value: `d:${draft.id}`,
      label: `Draft ${draft.declared_version ?? ""} (revision ${String(draft.revision)})`,
    })),
  ];
  return (
    <div className={styles.formRow}>
      <TextField
        id="new-source-search"
        label="Find a contract"
        type="search"
        autoComplete="off"
        value={q}
        onChange={(event) => {
          setQ(event.target.value);
        }}
      />
      <SelectField
        id="new-source-contract"
        label="Source contract"
        placeholder="Choose a contract"
        options={(contracts.data ?? []).map((item) => ({
          value: item.id,
          label: `${item.name ?? item.odcs_id} (${item.odcs_id})`,
        }))}
        value={sourceId}
        onChange={(event) => {
          setSourceId(event.target.value);
          setSourceRef("");
        }}
      />
      <SelectField
        id="new-source-ref"
        label="Version or draft"
        placeholder="Choose a version or draft"
        disabled={sourceId === ""}
        options={refOptions}
        value={sourceRef}
        onChange={(event) => {
          setSourceRef(event.target.value);
        }}
      />
    </div>
  );
}

function Section({ children }: { children: ReactNode }) {
  return <div className={styles.stackTight}>{children}</div>;
}

export function NewContractPage({ search }: { search: NewContractSearch }) {
  const held = useHeldAccess();
  const api = useApi();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const folders = useFolderList(false).data?.folders ?? [];
  // The platform's default ODCS version for a blank document (falls back to v3.2.0 on a failure).
  const publicSettings = usePublicSettings();
  const candidates = folders
    .filter((candidate) => !candidate.archived)
    .sort(
      (a, b) =>
        Number(b.kind === "root") - Number(a.kind === "root") ||
        a.path.localeCompare(b.path),
    );
  const [folderId, setFolderId] = useState(search.folder ?? "");
  const folder = candidates.find((candidate) => candidate.id === folderId);
  // Only a hint (D-UI-08-21): the API still decides, this avoids a pointless 403.
  const capabilities = useCapabilities(folder?.id, {
    enabled: folder !== undefined,
  });
  const cannotCreate =
    folder !== undefined &&
    capabilities.data !== undefined &&
    !capabilities.can("contract:create")
      ? CANNOT_CREATE
      : undefined;
  const [source, setSource] = useState<Source>(
    search.template ? "template" : "starter",
  );
  const [name, setName] = useState("");
  const [odcsId, setOdcsId] = useState("");
  const [odcsTouched, setOdcsTouched] = useState(false);
  const [format, setFormat] = useState<DocFormat>("yaml");
  const [blankText, setBlankText] = useState<string | null>(null);
  const [templateId, setTemplateId] = useState(search.template ?? "");
  const [templateVersion, setTemplateVersion] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [upload, setUpload] = useState<{
    name: string;
    text: string;
    mediaType: string;
  } | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState(false);
  const [invalid, setInvalid] = useState<string | null>(null);

  const effectiveOdcs = odcsTouched ? odcsId : suggestOdcsId(folder, name);
  const nameError =
    source === "starter" || source === "template"
      ? nameRuleError(name)
      : undefined;
  const blank =
    blankText ??
    blankSkeleton(
      format,
      effectiveOdcs,
      name || "New contract",
      publicSettings.data?.default_odcs_api_version ?? FALLBACK_API_VERSION,
    );

  async function create(): Promise<ContractCreated> {
    const path = { folder_id: folderId };
    if (source === "starter") {
      const { data } = await api.POST(
        "/api/v1/contracts/folders/{folder_id}/contracts/starter",
        {
          params: { path },
          body: { odcs_id: effectiveOdcs, name: name.trim(), notation: "odcs" },
        },
      );
      if (!data) throw new Error("no body");
      return data;
    }
    if (source === "template") {
      const { data } = await api.POST(
        "/api/v1/contracts/folders/{folder_id}/contracts/from-template",
        {
          params: { path },
          body: {
            template_id: templateId,
            odcs_id: effectiveOdcs,
            // The template's own name stays unless the author typed one.
            ...(name.trim() ? { overrides: { name: name.trim() } } : {}),
            ...(templateVersion
              ? { template_version: Number(templateVersion) }
              : {}),
          },
        },
      );
      if (!data) throw new Error("no body");
      return data;
    }
    if (source === "version") {
      const [kind, ref] = [sourceRef.slice(0, 1), sourceRef.slice(2)];
      const { data } = await api.POST(
        "/api/v1/contracts/folders/{folder_id}/contracts/from-version",
        {
          params: { path },
          body: {
            source_contract_id: sourceId,
            odcs_id: effectiveOdcs,
            ...(kind === "d"
              ? { source_draft_id: ref }
              : { source_version: ref }),
          },
        },
      );
      if (!data) throw new Error("no body");
      return data;
    }
    const text = source === "upload" ? (upload?.text ?? "") : blank;
    const mediaType =
      source === "upload"
        ? (upload?.mediaType ?? "application/yaml")
        : format === "json"
          ? "application/json"
          : "application/yaml";
    const { data } = await api.POST(
      "/api/v1/contracts/folders/{folder_id}/contracts",
      {
        params: { path },
        body: text,
        bodySerializer: (body: string) => body,
        headers: { "Content-Type": mediaType },
      },
    );
    if (!data) throw new Error("no body");
    return data;
  }

  function validateForm(): string | null {
    if (!folder) return "Choose a folder.";
    if (cannotCreate) return cannotCreate;
    if (source === "starter" && name.trim() === "") return "Enter a name.";
    // The rule's message is already under the field; the form-level line only points at it.
    if (nameError !== undefined)
      return "Fix the name before creating the contract.";
    if (
      (source === "starter" || source === "template" || source === "version") &&
      effectiveOdcs.trim() === ""
    )
      return "Enter an ODCS id.";
    if (source === "template" && templateId === "") return "Choose a template.";
    if (source === "version" && (sourceId === "" || sourceRef === ""))
      return "Choose a source contract and a version or draft.";
    if (source === "upload" && !upload) return "Choose a file to upload.";
    return null;
  }

  async function submit() {
    const message = validateForm();
    setInvalid(message);
    if (message) return;
    setBusy(true);
    setProblem(null);
    try {
      const created = await create();
      await queryClient.invalidateQueries({ queryKey: contractKeys.lists });
      await navigate({
        to: "/contracts/$contractId/drafts/$draftId",
        params: { contractId: created.contract.id, draftId: created.draft.id },
      });
    } catch (error) {
      setProblem(problemOf(error));
    } finally {
      setBusy(false);
    }
  }

  const needsIdentity =
    source === "starter" || source === "template" || source === "version";

  return (
    <>
      <PageHeader
        title="New contract"
        breadcrumbs={[
          { label: "Contracts", to: "/contracts" },
          { label: "New contract" },
        ]}
        description="Contracts can live in any folder: Root, an organization, a project or a team. Pick the folder, then how to start."
      />
      {held.data !== undefined && !holdsAny(held.data, "contract:create") ? (
        <NotAvailable what="Creating contracts" />
      ) : (
        <form
          className={styles.form}
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <SelectField
            id="new-folder"
            label="Folder"
            placeholder="Choose a folder"
            helper={cannotCreate}
            options={candidates.map((candidate) => ({
              value: candidate.id,
              label: candidate.kind === "root" ? "Root" : candidate.path,
            }))}
            value={folderId}
            onChange={(event) => {
              setFolderId(event.target.value);
            }}
          />
          <SourcePicker value={source} onChange={setSource} />
          {needsIdentity ? (
            <div className={styles.formRow}>
              {source === "starter" || source === "template" ? (
                <TextField
                  id="new-name"
                  autoComplete="off"
                  label="Name"
                  helper={
                    source === "template"
                      ? "Optional: replaces the template's name."
                      : undefined
                  }
                  maxLength={200}
                  value={name}
                  error={nameError}
                  onChange={(event) => {
                    setName(event.target.value);
                  }}
                />
              ) : null}
              <TextField
                id="new-odcs-id"
                autoComplete="off"
                label="ODCS id"
                helper="Unique within the organization (or among the contracts at Root), for example urn:acme:payments:orders."
                maxLength={ODCS_ID_MAX_LENGTH}
                value={effectiveOdcs}
                onChange={(event) => {
                  setOdcsTouched(true);
                  setOdcsId(event.target.value);
                }}
              />
            </div>
          ) : null}
          {source === "blank" ? (
            <Section>
              <SelectField
                id="new-format"
                label="Format"
                options={[
                  { value: "yaml", label: "YAML" },
                  { value: "json", label: "JSON" },
                ]}
                value={format}
                onChange={(event) => {
                  setFormat(event.target.value === "json" ? "json" : "yaml");
                  setBlankText(null);
                }}
              />
              <CodeEditor
                value={blank}
                onChange={setBlankText}
                language={format}
                label="Contract document"
                fit="tall"
              />
            </Section>
          ) : null}
          {source === "template" && folderId ? (
            <TemplateFields
              folderId={folderId}
              templateId={templateId}
              setTemplateId={setTemplateId}
              templateVersion={templateVersion}
              setTemplateVersion={setTemplateVersion}
            />
          ) : null}
          {source === "template" && !folderId ? (
            <p className={styles.muted}>
              Choose a folder to see its templates.
            </p>
          ) : null}
          {source === "version" ? (
            <VersionFields
              sourceId={sourceId}
              setSourceId={setSourceId}
              sourceRef={sourceRef}
              setSourceRef={setSourceRef}
            />
          ) : null}
          {source === "upload" ? (
            <Section>
              <label htmlFor="new-upload" className={styles.sourceTitle}>
                Contract file
              </label>
              <input
                id="new-upload"
                type="file"
                accept=".yaml,.yml,.json,application/json,application/yaml"
                className={styles.fileInput}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  setUpload(null);
                  setUploadError(null);
                  if (!file) return;
                  if (file.size > MAX_UPLOAD_BYTES) {
                    setUploadError(
                      "The file is larger than 2 MiB. Upload a smaller document.",
                    );
                    return;
                  }
                  const json = file.name.toLowerCase().endsWith(".json");
                  void file.text().then((text) => {
                    setUpload({
                      name: file.name,
                      text,
                      mediaType: json ? "application/json" : "application/yaml",
                    });
                  });
                }}
              />
              {uploadError ? (
                <Alert intent="danger">{uploadError}</Alert>
              ) : null}
              {upload ? (
                <CodeEditor
                  value={upload.text}
                  language={upload.mediaType.includes("json") ? "json" : "yaml"}
                  readOnly
                  label={`Preview of ${upload.name}`}
                  fit="tall"
                />
              ) : null}
            </Section>
          ) : null}
          {invalid ? <Alert intent="danger">{invalid}</Alert> : null}
          {problem ? <ProblemAlert problem={problem} context="action" /> : null}
          <div className={styles.toolbar}>
            <Button
              type="submit"
              variant="primary"
              size="small"
              busy={busy}
              busyLabel="Creating…"
            >
              Create contract
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="small"
              onClick={() => void navigate({ to: "/contracts" })}
            >
              Go back
            </Button>
          </div>
        </form>
      )}
    </>
  );
}
