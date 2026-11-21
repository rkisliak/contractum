// The full-screen editor: "Expand editor" on a page opens the same document, on the same buffer,
// in a Dialog that covers the viewport minus a 16px margin. The caller passes its own toolbar
// (format toggle and downloads for the viewer, save, validate and download for a draft) and the
// notices to show above the editor; the editor props are the page editor's, so edits made here are
// the page's edits. Esc or "Exit full screen" closes it; focus is trapped while open and returns
// to "Expand editor" (Dialog). The caller unmounts its page editor while this is open, so there is
// never a second live Monaco instance on one buffer.
import { Maximize2, Minimize2 } from "lucide-react";
import type { ReactNode } from "react";
import { IconButton } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { Skeleton } from "@/components/states";
import { Tooltip } from "@/components/Tooltip";
import { CodeEditor, type CodeEditorProps } from "./CodeEditor";
import styles from "./CodeEditor.module.css";

export function ExpandEditorButton({ onClick }: { onClick: () => void }) {
  return (
    <Tooltip content="Expand editor" side="bottom" align="end">
      <IconButton
        icon={Maximize2}
        size={36}
        variant="secondary"
        aria-label="Expand editor"
        onClick={onClick}
      />
    </Tooltip>
  );
}

export type EditorDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** "{contract name} · Document" or "{contract name} · Draft {semver}". */
  title: string;
  /** The action row above the editor. */
  toolbar: ReactNode;
  /** Status lines and alerts between the toolbar and the editor. */
  notices?: ReactNode;
  /** The page editor's props, or null while the document is still loading. */
  editor: Omit<CodeEditorProps, "fit" | "height" | "ref"> | null;
};

export function EditorDialog({
  open,
  onOpenChange,
  title,
  toolbar,
  notices,
  editor,
}: EditorDialogProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      size="full"
      closeIcon={Minimize2}
      closeLabel="Exit full screen"
    >
      <div className={styles.dialogToolbar}>{toolbar}</div>
      {notices}
      {editor ? (
        <CodeEditor {...editor} fit="container" />
      ) : (
        <Skeleton label="document" rows={8} />
      )}
    </Dialog>
  );
}
