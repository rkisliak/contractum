// /rule-sets?folder_id=: pick a folder (ROOT by default, else the first folder the caller can
// see) and see the rule sets defined there and the ones it inherits.
import type { Folder } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { useFolderList } from "@/features/folders/api";
import { FolderSelect } from "@/features/folders/FolderSelect";
import { problemOf } from "@/lib/unexpected";
import { RuleSetLists } from "./RuleSetLists";
import styles from "@/features/governance/gov.module.css";

export function defaultFolder(folders: Folder[]): Folder | undefined {
  return (
    folders.find((folder) => folder.kind === "root") ??
    [...folders].sort((a, b) => a.path.localeCompare(b.path))[0]
  );
}

export function RuleSetsPage({
  folderId,
  onFolderChange,
}: {
  folderId: string | undefined;
  onFolderChange: (folderId: string) => void;
}) {
  const list = useFolderList();
  const folders = list.data?.folders ?? [];
  const selected =
    folders.find((folder) => folder.id === folderId) ?? defaultFolder(folders);
  return (
    <>
      <PageHeader
        title="Rule sets"
        description="Declarative rules that check contracts. A rule set published at a folder can govern every contract below it; a mandatory one blocks publishes that break it."
        breadcrumbs={[{ label: "Governance" }]}
      />
      <div className={styles.stack}>
        <div className={styles.toolbar}>
          <div className={styles.toolbarField}>
            <FolderSelect
              id="rule-sets-folder"
              label="Folder"
              value={selected?.id ?? ""}
              onChange={onFolderChange}
            />
          </div>
        </div>
        {list.isError ? (
          <ProblemAlert
            problem={problemOf(list.error)}
            onRetry={() => void list.refetch()}
          />
        ) : selected === undefined ? (
          list.isPending ? (
            <Skeleton label="folders" rows={2} />
          ) : (
            <p className={styles.muted}>You cannot see any folder yet.</p>
          )
        ) : (
          <RuleSetLists key={selected.id} folder={selected} />
        )}
      </div>
    </>
  );
}
