// "Download YAML" and "Download JSON" (D-UI-10-16): fetch with Accept through the authenticated
// client and save through a Blob. The filename is <odcs_id>-<label>.<ext>.
import { Download } from "lucide-react";
import { useState } from "react";
import type { Problem } from "@/api/problem";
import { Button } from "@/components/Button";
import { ProblemAlert } from "@/components/ProblemAlert";
import { problemOf } from "@/lib/unexpected";
import {
  ACCEPT,
  fileNameFor,
  saveText,
  type DocFormat,
  type TextDocument,
} from "./api";
import styles from "./Contracts.module.css";

export function DocumentDownloads({
  fetchDoc,
  odcsId,
  label,
}: {
  fetchDoc: (format: DocFormat) => Promise<TextDocument>;
  odcsId: string;
  label: string;
}) {
  const [busy, setBusy] = useState<DocFormat | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  async function download(format: DocFormat) {
    setBusy(format);
    setProblem(null);
    try {
      const doc = await fetchDoc(format);
      saveText(doc.text, fileNameFor(odcsId, label, format), ACCEPT[format]);
    } catch (error) {
      setProblem(problemOf(error));
    } finally {
      setBusy(null);
    }
  }
  return (
    <>
      <div className={styles.toolbar}>
        <Button
          variant="secondary"
          size="small"
          icon={Download}
          busy={busy === "yaml"}
          busyLabel="Downloading…"
          onClick={() => void download("yaml")}
        >
          Download YAML
        </Button>
        <Button
          variant="secondary"
          size="small"
          icon={Download}
          busy={busy === "json"}
          busyLabel="Downloading…"
          onClick={() => void download("json")}
        >
          Download JSON
        </Button>
      </div>
      {problem ? <ProblemAlert problem={problem} context="action" /> : null}
    </>
  );
}

export function FormatToggle({
  value,
  onChange,
  id,
}: {
  value: DocFormat | "stored";
  onChange: (next: DocFormat | "stored") => void;
  id: string;
}) {
  return (
    <div
      role="group"
      aria-label="Document format"
      className={styles.toolbar}
      id={id}
    >
      {(["stored", "yaml", "json"] as const).map((option) => (
        <Button
          key={option}
          variant={value === option ? "primary" : "outline-neutral"}
          size="small"
          aria-pressed={value === option}
          onClick={() => {
            onChange(option);
          }}
        >
          {option === "stored" ? "As stored" : option.toUpperCase()}
        </Button>
      ))}
    </div>
  );
}
