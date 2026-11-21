// Import settings (quick 261120-0yc, P4): choose a Contractum settings export (.json, at most
// 256 KiB), see the dry-run diff, then Apply. The API validates the file (unknown format, version,
// section or field is a 422 with a JSON pointer); Apply sends the combined ETag the dry run
// returned, so a change made meanwhile is a 412 and nothing is applied.
import { useState } from "react";
import type { SettingsChange } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { DataTable, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState } from "@/components/states";
import { useToast } from "@/components/Toast";
import { problemOf } from "@/lib/unexpected";
import { useImportSettings } from "./api";
import { isStale } from "./SaveProblem";
import styles from "./settings.module.css";

export const IMPORT_MAX_BYTES = 256 * 1024;

const show = (value: unknown): string =>
  value === null || value === undefined ? "default" : JSON.stringify(value);

const columns: Column<SettingsChange>[] = [
  { id: "section", header: "Section", rowHeader: true, cell: (r) => r.section },
  { id: "field", header: "Setting", cell: (r) => r.field },
  {
    id: "before",
    header: "Current",
    cell: (r) => <span className={styles.ident}>{show(r.before)}</span>,
  },
  {
    id: "after",
    header: "New",
    cell: (r) => <span className={styles.ident}>{show(r.after)}</span>,
  },
];

export function ImportSettingsDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const run = useImportSettings();
  const toast = useToast();
  const [text, setText] = useState<string | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [changes, setChanges] = useState<SettingsChange[] | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  function reset() {
    setText(null);
    setFileError(null);
    setChanges(null);
    setEtag(null);
    setError(null);
    run.reset();
  }

  async function onFile(file: File | undefined) {
    reset();
    if (file === undefined) return;
    if (file.size > IMPORT_MAX_BYTES) {
      setFileError("This file is larger than 256 KiB.");
      return;
    }
    const content = await file.text();
    setText(content);
    run.mutate(
      { text: content, apply: false },
      {
        onSuccess: (result) => {
          setChanges(result.changes);
          setEtag(result.etag);
        },
        onError: (failure) => {
          setError(failure);
        },
      },
    );
  }

  function apply() {
    if (text === null || etag === null) return;
    setError(null);
    run.mutate(
      { text, apply: true, etag },
      {
        onSuccess: () => {
          toast({ title: "Settings imported.", intent: "success" });
          onOpenChange(false);
          reset();
        },
        onError: (failure) => {
          setError(failure);
        },
      },
    );
  }

  const problem = error === null ? null : problemOf(error);
  const canApply = changes !== null && changes.length > 0 && etag !== null;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Import settings"
      size={896}
      description="Choose a settings file exported from Contractum. You see what would change before anything is applied."
      footer={
        <>
          <Button
            variant="outline-neutral"
            size="small"
            onClick={() => {
              onOpenChange(false);
              reset();
            }}
          >
            Cancel
          </Button>
          <Button
            variant="primary"
            size="small"
            disabled={!canApply}
            busy={run.isPending && changes !== null}
            busyLabel="Applying…"
            onClick={apply}
          >
            Apply
          </Button>
        </>
      }
    >
      <div className={styles.stack}>
        <div>
          <label htmlFor="settings-import-file">Settings file (.json)</label>
          <br />
          <input
            id="settings-import-file"
            className={styles.fileInput}
            type="file"
            accept=".json,application/json"
            onChange={(event) => {
              void onFile(event.target.files?.[0]);
            }}
          />
        </div>
        {fileError ? <Alert intent="danger">{fileError}</Alert> : null}
        {problem !== null && isStale(problem) ? (
          <Alert intent="warning" title="These settings changed elsewhere">
            Settings changed after you chose the file. Choose the file again to
            see the current differences.
          </Alert>
        ) : problem !== null ? (
          <ProblemAlert problem={problem} context="action" />
        ) : null}
        {changes !== null && changes.length === 0 ? (
          <EmptyState
            level={3}
            heading="No changes"
            body="This file matches the current settings."
          />
        ) : null}
        {changes !== null && changes.length > 0 ? (
          <DataTable
            caption="Changes this file would make"
            columns={columns}
            rows={changes}
            getRowId={(row) => `${row.section}/${row.field}`}
          />
        ) : null}
      </div>
    </Dialog>
  );
}
