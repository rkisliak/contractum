// A folder picker over the shared folder list (governance screens: templates, rule sets, dry-run
// and drift). Options are ordered by path and labelled with the readable path, ROOT first as
// "Root". `filter` narrows the options (for example, only a rule set's subtree).
import type { Folder } from "@/api/types";
import { SelectField } from "@/components/form";
import { useFolderList } from "./api";

export function folderOptionLabel(folder: Folder): string {
  if (folder.kind === "root") return "Root (all organizations)";
  return `${folder.display_name} (${folder.path})`;
}

/** The label for a folder id the caller cannot read (see `OwningFolder`). */
export const HIDDEN_FOLDER_LABEL = "Folder you cannot see";

/** The readable path of a folder id, for table cells ("Platform" when there is none). */
export function useFolderPathLabel() {
  const list = useFolderList();
  const byId = new Map(
    (list.data?.folders ?? []).map((folder) => [folder.id, folder]),
  );
  return (folderId: string | null | undefined): string => {
    if (folderId === null || folderId === undefined) return "Platform";
    const folder = byId.get(folderId);
    if (folder === undefined) return HIDDEN_FOLDER_LABEL;
    return folder.kind === "root" ? "Root" : folder.path;
  };
}

export function useRootFolder(): Folder | undefined {
  const list = useFolderList();
  return list.data?.folders.find((folder) => folder.kind === "root");
}

export function FolderSelect({
  id,
  label,
  value,
  onChange,
  placeholder,
  filter,
  helper,
  controlSize = "small",
  labelHidden = false,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (folderId: string) => void;
  placeholder?: string;
  filter?: (folder: Folder) => boolean;
  helper?: string;
  controlSize?: "small" | "default";
  labelHidden?: boolean;
}) {
  const list = useFolderList();
  const folders = (list.data?.folders ?? [])
    .filter((folder) => filter === undefined || filter(folder))
    .sort((a, b) => a.path.localeCompare(b.path));
  return (
    <SelectField
      id={id}
      label={label}
      labelHidden={labelHidden}
      helper={helper}
      controlSize={controlSize}
      value={value}
      placeholder={placeholder}
      disabled={list.isPending}
      options={folders.map((folder) => ({
        value: folder.id,
        label: folderOptionLabel(folder),
      }))}
      onChange={(event) => {
        onChange(event.target.value);
      }}
    />
  );
}
