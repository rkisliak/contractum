// /rule-sets/$ruleSetId?via=&tab= (D-UI-11-06): the header (name, scope, owning folder,
// mandatory badge and the rollout status of the newest version, so "when does this start
// blocking" is in plain sight) and four tabs: Draft, Versions, Rollout and Impact.
//
// The owner routes (GET /rule-sets/{id}) need ruleset:read at the owning folder. Anyone below
// can still read a published set through `available-rule-sets/{id}` at a folder it governs
// (`via`, or ROOT for platform sets): the page then shows the read-only tabs (UI-03). When `via`
// is given and the caller lacks ruleset:read there, it cannot hold it at the owning folder above
// either, so the owner request is skipped instead of probing a 404.
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { SelectField } from "@/components/form";
import { NotFound } from "@/components/NotFound";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { Tabs } from "@/components/Tabs";
import { useCapabilities } from "@/features/access/useCapabilities";
import { useFolderList } from "@/features/folders/api";
import {
  useFolderPathLabel,
  useRootFolder,
} from "@/features/folders/FolderSelect";
import { OwningFolder } from "@/features/folders/OwningFolder";
import {
  DryRunResults,
  type DryRunRequest,
} from "@/features/governance/DryRunResults";
import { isNotFound, problemOf } from "@/lib/unexpected";
import {
  useAvailableRuleSet,
  useRuleSetDraft,
  useRuleSetVersions,
} from "./api";
import { RuleDraftEditor } from "./RuleDraftEditor";
import { RuleSetRollout } from "./RuleSetRollout";
import { RuleSetVersions } from "./RuleSetVersions";
import { platformNote, rolloutStatus, scopeLabel } from "./rollout";
import styles from "@/features/governance/gov.module.css";

export type RuleSetTab = "draft" | "versions" | "rollout" | "impact";

export function RuleSetPage({
  ruleSetId,
  via,
  tab,
  onTabChange,
}: {
  ruleSetId: string;
  via: string | undefined;
  tab: RuleSetTab | undefined;
  onTabChange: (tab: RuleSetTab) => void;
}) {
  const root = useRootFolder();
  const foldersKnown = useFolderList().data !== undefined;
  const pathOf = useFolderPathLabel();
  const viaCaps = useCapabilities(via ?? null);
  const skipOwner =
    via !== undefined && !viaCaps.isPending && !viaCaps.can("ruleset:read");
  const draft = useRuleSetDraft(
    ruleSetId,
    via === undefined || (!viaCaps.isPending && !skipOwner),
  );
  const [editorKey, setEditorKey] = useState(0);
  const owner = draft.data?.rule_set;
  const ownerMissing = skipOwner || (draft.isError && isNotFound(draft.error));
  const viewFolder =
    via ?? owner?.folder_id ?? (ownerMissing ? root?.id : undefined) ?? null;
  const published = owner ? owner.publish_counter > 0 : ownerMissing;
  const available = useAvailableRuleSet(viewFolder, ruleSetId, published);
  const versions = useRuleSetVersions(ruleSetId, owner !== undefined);
  const folderCaps = useCapabilities(owner?.folder_id ?? root?.id ?? null);

  if (draft.isError && !ownerMissing)
    return (
      <ProblemAlert
        problem={problemOf(draft.error)}
        onRetry={() => void draft.refetch()}
      />
    );
  if (
    ownerMissing &&
    (available.isError || (viewFolder === null && foldersKnown))
  )
    return <NotFound back={{ label: "rule sets", to: "/rule-sets" }} />;
  if (
    (!skipOwner && draft.isPending) ||
    (ownerMissing && available.data === undefined)
  )
    return <Skeleton label="rule set" rows={5} />;

  const info = owner ?? available.data;
  if (info === undefined) return <Skeleton label="rule set" rows={5} />;
  const readOnly = owner === undefined;
  const isPlatform = info.folder_id === null;
  const folderLabel = isPlatform ? "every folder" : pathOf(info.folder_id);
  const resolved = available.data?.resolved ?? [];
  const paused = available.data?.paused ?? false;
  const status = rolloutStatus(info.mandatory, resolved, paused);
  const versionList = readOnly
    ? (available.data?.versions ?? [])
    : (versions.data ?? []);
  const canEnforce =
    folderCaps.can("ruleset:mandate") && (!readOnly || isPlatform);
  const note = platformNote({
    isPlatform,
    resolved,
    versions: versionList,
    paused,
    pausedAt: available.data?.paused_at,
  });

  const impactItem = {
    value: "impact",
    label: "Impact",
    content: isPlatform ? (
      <Alert intent="info">
        Platform rule sets apply everywhere and cannot be dry-run. Use the Drift
        report to see contracts that do not pass them.
      </Alert>
    ) : (
      <ImpactTab
        ruleSetId={ruleSetId}
        folderId={info.folder_id}
        versions={versionList
          .filter((v) => v.deprecated_at === null)
          .map((v) => v.version_no)}
        allowDraft={!readOnly}
      />
    ),
  };
  const items = [
    ...(readOnly || draft.data === undefined
      ? []
      : [
          {
            value: "draft",
            label: "Draft",
            content: (
              <RuleDraftEditor
                key={editorKey}
                draft={draft.data}
                onRefetch={() => {
                  void draft.refetch().then(() => {
                    setEditorKey((value) => value + 1);
                  });
                }}
              />
            ),
          },
        ]),
    {
      value: "versions",
      label: "Versions",
      content:
        !readOnly && versions.isError ? (
          <ProblemAlert problem={problemOf(versions.error)} />
        ) : (
          <RuleSetVersions
            ruleSetId={ruleSetId}
            name={info.name}
            versions={versionList}
            via={readOnly ? viewFolder : null}
            canDeprecate={!readOnly && folderCaps.can("ruleset:deprecate")}
          />
        ),
    },
    {
      value: "rollout",
      label: "Rollout",
      content: (
        <RuleSetRollout
          ruleSetId={ruleSetId}
          name={info.name}
          folderLabel={folderLabel}
          mandatory={info.mandatory}
          versions={versionList}
          resolved={resolved}
          paused={paused}
          canMandate={
            !readOnly && !isPlatform && folderCaps.can("ruleset:mandate")
          }
          canEnforce={canEnforce}
          canPause={
            isPlatform &&
            root !== undefined &&
            folderCaps.can("ruleset:mandate")
          }
          canReschedule={canEnforce}
          {...(isPlatform && root ? { platformRootId: root.id } : {})}
        />
      ),
    },
    ...(isPlatform || (readOnly ? viaCaps : folderCaps).can("ruleset:dry-run")
      ? [impactItem]
      : []),
  ];
  const current = items.some((item) => item.value === tab)
    ? tab
    : items[0]?.value;

  return (
    <>
      <PageHeader
        title={info.name}
        breadcrumbs={[
          { label: "Governance" },
          { label: "Rule sets", to: "/rule-sets" },
          { label: info.name },
        ]}
        badges={
          <>
            <Badge intent="brand">
              {scopeLabel(info.scope, info.folder_kind)}
            </Badge>
            {info.mandatory ? <Badge intent="warning">Mandatory</Badge> : null}
            <Badge intent={status.intent}>{status.label}</Badge>
            {readOnly ? <Badge intent="neutral">Read only</Badge> : null}
          </>
        }
        description={
          <>
            {info.description ? <>{info.description} </> : null}
            Owned by{" "}
            {isPlatform ? (
              "the platform"
            ) : (
              <OwningFolder folderId={info.folder_id} />
            )}
            {note === null ? null : (
              <>
                <br />
                <span className={styles.muted}>{note}</span>
              </>
            )}
          </>
        }
      />
      <Tabs
        label="Rule set sections"
        items={items}
        value={current}
        onValueChange={(value) => {
          onTabChange(value as RuleSetTab);
        }}
      />
    </>
  );
}

function ImpactTab({
  ruleSetId,
  folderId,
  versions,
  allowDraft,
}: {
  ruleSetId: string;
  folderId: string | null;
  versions: number[];
  allowDraft: boolean;
}) {
  const sorted = [...versions].sort((a, b) => b - a);
  const [version, setVersion] = useState(
    sorted[0] !== undefined ? String(sorted[0]) : allowDraft ? "draft" : "",
  );
  const [request, setRequest] = useState<DryRunRequest | null>(null);
  const options = [
    ...sorted.map((n) => ({ value: String(n), label: `Version ${String(n)}` })),
    ...(allowDraft ? [{ value: "draft", label: "Draft (unpublished)" }] : []),
  ];
  return (
    <div className={styles.stack}>
      <p className={styles.muted}>
        Run the rules against the published contracts below the owning folder.
        Nothing is changed. For a narrower scope, use{" "}
        <Link
          to="/governance/dry-run"
          search={{
            rule_set_id: ruleSetId,
            ...(folderId ? { folder_id: folderId } : {}),
          }}
          className={styles.rowLink}
        >
          the Dry runs page
        </Link>
        .
      </p>
      <form
        className={styles.toolbar}
        aria-label="Dry-run settings"
        onSubmit={(event) => {
          event.preventDefault();
          if (version === "") return;
          setRequest((previous) => ({
            ruleSetId,
            version: version === "draft" ? "draft" : Number(version),
            ...(folderId ? { folderId } : {}),
            run: (previous?.run ?? 0) + 1,
          }));
        }}
      >
        <div className={styles.toolbarField}>
          <SelectField
            id="impact-version"
            label="Version"
            controlSize="small"
            placeholder="Choose a version"
            value={version}
            options={options}
            onChange={(event) => {
              setVersion(event.target.value);
            }}
          />
        </div>
        <Button
          variant="primary"
          size="small"
          type="submit"
          disabled={version === ""}
        >
          Run dry-run
        </Button>
      </form>
      {request ? <DryRunResults request={request} /> : null}
    </div>
  );
}
