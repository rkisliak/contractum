// The owning folder of a governed object (template, rule set) in a table cell or a sentence:
// the folder's path in body text (wrapping only after a "/"), "Platform" for a platform-wide
// object and a muted "Folder you cannot see" when the caller cannot read that folder.
import { Breakable } from "@/components/Breakable";
import { useFolderPathLabel, HIDDEN_FOLDER_LABEL } from "./FolderSelect";
import styles from "./OwningFolder.module.css";

export function OwningFolder({
  folderId,
}: {
  folderId: string | null | undefined;
}) {
  const pathOf = useFolderPathLabel();
  const label = pathOf(folderId);
  if (label === HIDDEN_FOLDER_LABEL)
    return <span className={styles.hidden}>{label}</span>;
  return (
    <span className={styles.path}>
      <Breakable text={label} />
    </span>
  );
}
