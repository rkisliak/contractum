// The Draft tab of a rule set (D-UI-11-07 to 09): the rule list on the start side, the selected
// rule's form on the end side. Saving a rule (PUT .../draft/rules/{key}) or removing one carries
// the draft's If-Match and brings back the server's advisory check; "Check draft" runs the full
// check. Publish is offered once the last check is clean for the current draft revision (the
// server enforces it anyway). "Edit draft as JSON" replaces the whole draft (PUT .../draft). A 412
// (someone saved first) shows the server's current rule (or draft) beside the refused change,
// then offers "Reload draft".
import { useNavigate } from "@tanstack/react-router";
import {
  CircleCheck,
  CircleDashed,
  CircleX,
  FileJson,
  Plus,
  ShieldCheck,
  Trash2,
  Upload,
} from "lucide-react";
import { useState } from "react";
import type { Problem } from "@/api/problem";
import type { RuleSetDraft } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Dialog } from "@/components/Dialog";
import { TextareaField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { DiffView } from "@/features/editor/CodeEditor";
import { problemOf } from "@/lib/unexpected";
import { useApi, useRuleSetWrites } from "./api";
import {
  describeProblem,
  parseCheck,
  ruleStatuses,
  statusOf,
  type CheckReport,
} from "./check";
import { RuleForm } from "./RuleForm";
import {
  emptyForm,
  formFromRule,
  ruleFromForm,
  type FormErrors,
  type RuleFormValues,
} from "./ruleValues";
import styles from "@/features/governance/gov.module.css";

const severityIntent = {
  error: "danger",
  warning: "warning",
  info: "neutral",
} as const;

/** The selection of the unsaved new rule; never a valid rule key (keys start with a letter). */
const NEW_RULE = "+new";

type LastCheck = { revision: number; report: CheckReport | null };

/** A refused (412) write: what the user tried to save, and the server's copy once loaded. */
type Conflict = {
  /** The rule key, or null when the whole draft was replaced. */
  key: string | null;
  mine: string;
  /** undefined while loading, null when it could not be loaded. */
  server?: { text: string; revision: number } | null;
};

const json = (value: unknown) => JSON.stringify(value, null, 2);

export function RuleDraftEditor({
  draft,
  onRefetch,
}: {
  draft: RuleSetDraft;
  onRefetch: () => void;
}) {
  const ruleSet = draft.rule_set;
  const navigate = useNavigate();
  const api = useApi();
  const caps = useCapabilities(ruleSet.folder_id);
  const writes = useRuleSetWrites(ruleSet.id);
  const rules = [...draft.rules].sort((a, b) => a.key.localeCompare(b.key));
  const [selected, setSelected] = useState<string | null>(
    rules[0]?.key ?? null,
  );
  const [values, setValues] = useState<RuleFormValues | null>(
    rules[0] ? formFromRule(rules[0]) : null,
  );
  const [errors, setErrors] = useState<FormErrors>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [lastCheck, setLastCheck] = useState<LastCheck | null>(null);
  const [dialog, setDialog] = useState<
    "delete-rule" | "publish" | "json" | "delete-set" | null
  >(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [jsonText, setJsonText] = useState("");
  const [jsonError, setJsonError] = useState<string | undefined>();

  const canEdit = caps.can("ruleset:update");
  const canPublish = caps.can("ruleset:publish");
  const revision = ruleSet.draft_revision;
  const report = lastCheck?.revision === revision ? lastCheck.report : null;
  const checkedClean = report?.status === "ok" && report.ok;
  const statuses = ruleStatuses(report);

  function select(key: string) {
    setSelected(key);
    setErrors({});
    setProblem(null);
    if (key === NEW_RULE) {
      setValues(emptyForm());
      return;
    }
    const rule = draft.rules.find((candidate) => candidate.key === key);
    setValues(rule ? formFromRule(rule) : null);
  }

  /** Shows the refused change, then loads the server's current copy beside it. */
  function openConflict(key: string | null, mine: string) {
    setConflict({ key, mine });
    api
      .GET("/api/v1/validation/rule-sets/{rule_set_id}", {
        params: { path: { rule_set_id: ruleSet.id } },
      })
      .then(({ data }) => {
        if (data === undefined) throw new Error("no body");
        const current =
          key === null
            ? json({
                description: data.rule_set.description,
                rules: data.rules,
              })
            : (() => {
                const rule = data.rules.find((r) => r.key === key);
                return rule ? json(rule) : "";
              })();
        return { text: current, revision: data.rule_set.draft_revision };
      })
      .then(
        (server) => {
          setConflict((open) =>
            open && open.mine === mine ? { ...open, server } : open,
          );
        },
        () => {
          setConflict((open) =>
            open && open.mine === mine ? { ...open, server: null } : open,
          );
        },
      );
  }

  function onWriteError(error: unknown, key: string, mine: string) {
    const found = problemOf(error);
    if (found.status === 412) {
      openConflict(key, mine);
      return;
    }
    setProblem(found);
  }

  function afterWrite(next: RuleSetDraft) {
    setLastCheck({
      revision: next.rule_set.draft_revision,
      report: parseCheck(next.check),
    });
  }

  function saveRule() {
    if (values === null) return;
    const built = ruleFromForm(values);
    if ("errors" in built) {
      setErrors(built.errors);
      return;
    }
    if (
      selected === NEW_RULE &&
      draft.rules.some((rule) => rule.key === built.rule.key)
    ) {
      setErrors({ key: "A rule with this key already exists in the draft." });
      return;
    }
    setErrors({});
    setProblem(null);
    setNotice(null);
    writes.putRule.mutate(
      { revision, rule: built.rule },
      {
        onSuccess: (next) => {
          afterWrite(next);
          setSelected(built.rule.key);
          setNotice(`Rule ${built.rule.key} is saved.`);
        },
        onError: (error) => {
          onWriteError(error, built.rule.key, json(built.rule));
        },
      },
    );
  }

  function runCheck() {
    setProblem(null);
    setNotice(null);
    writes.check.mutate(undefined, {
      onSuccess: (next) => {
        afterWrite(next);
      },
      onError: (error) => {
        setProblem(problemOf(error));
      },
    });
  }

  function openJson() {
    setJsonText(
      JSON.stringify(
        { description: ruleSet.description, rules: draft.rules },
        null,
        2,
      ),
    );
    setJsonError(undefined);
    setDialog("json");
  }

  function saveJson() {
    let parsed: unknown;
    try {
      parsed = JSON.parse(jsonText);
    } catch {
      setJsonError("This is not valid JSON.");
      return;
    }
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !Array.isArray((parsed as { rules?: unknown }).rules)
    ) {
      setJsonError('The draft is an object with a "rules" array.');
      return;
    }
    setJsonError(undefined);
    writes.putDraft.mutate(
      {
        revision,
        draft: parsed as Parameters<typeof writes.putDraft.mutate>[0]["draft"],
      },
      {
        onSuccess: (next) => {
          afterWrite(next);
          setDialog(null);
          const first = [...next.rules].sort((a, b) =>
            a.key.localeCompare(b.key),
          )[0];
          setSelected(first?.key ?? null);
          setValues(first ? formFromRule(first) : null);
          setNotice("The draft is replaced.");
        },
        onError: (error) => {
          const found = problemOf(error);
          if (found.status === 412) {
            setDialog(null);
            openConflict(null, json(parsed));
            return;
          }
          if (found.kind !== "problem") {
            setJsonError("The server could not be reached. Try again.");
            return;
          }
          setJsonError(
            found.errors.length > 0
              ? found.errors.map((e) => `${e.pointer}: ${e.detail}`).join("; ")
              : (found.detail ?? found.title),
          );
        },
      },
    );
  }

  const selectedProblems =
    selected !== null && selected !== NEW_RULE
      ? (statuses.get(selected)?.problems ?? [])
      : [];

  return (
    <div className={styles.stack}>
      {conflict ? (
        <Alert intent="warning" title="Someone else changed this draft">
          <p className={styles.muted}>
            Your change was not saved. Compare it with the current server copy,
            reload the draft, then apply your change again.
          </p>
          {conflict.server === undefined ? (
            <p className={styles.muted} role="status">
              Loading the server copy…
            </p>
          ) : conflict.server === null ? (
            <p className={styles.muted}>
              The server copy could not be loaded for comparison.
            </p>
          ) : (
            <DiffView
              original={
                conflict.server.text ||
                `Rule ${conflict.key ?? ""} is not in the server draft.`
              }
              modified={conflict.mine}
              language="json"
              height="20rem"
              originalLabel={`Server ${conflict.key === null ? "draft" : `rule ${conflict.key}`}, revision ${String(conflict.server.revision)}`}
              modifiedLabel="Your refused change"
            />
          )}
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              setConflict(null);
              onRefetch();
            }}
          >
            Reload draft
          </Button>
        </Alert>
      ) : null}
      {problem ? <ProblemAlert problem={problem} context="action" /> : null}
      {notice ? (
        <Alert
          intent="success"
          onDismiss={() => {
            setNotice(null);
          }}
        >
          {notice}
        </Alert>
      ) : null}
      {report?.status === "unavailable" ? (
        <Alert intent="warning">
          The rule check is unavailable right now. Run Check draft again in a
          moment.
        </Alert>
      ) : null}
      {report?.status === "ok" ? (
        report.ok ? (
          <Alert intent="success">
            The draft passes its check. You can publish it.
          </Alert>
        ) : (
          <Alert intent="danger" title="The draft does not pass its check">
            {report.ruleSetProblems.length > 0 ? (
              <ul className={styles.problemList}>
                {report.ruleSetProblems.map((item) => (
                  <li key={item.code}>{describeProblem(item)}</li>
                ))}
              </ul>
            ) : (
              "Open the rules marked with problems to see what to fix."
            )}
          </Alert>
        )
      ) : null}

      <div className={styles.actions}>
        <Button
          variant="secondary"
          size="small"
          icon={ShieldCheck}
          busy={writes.check.isPending}
          busyLabel="Checking…"
          disabled={draft.rules.length === 0}
          onClick={runCheck}
        >
          Check draft
        </Button>
        {canPublish ? (
          <Button
            variant="primary"
            size="small"
            icon={Upload}
            disabled={!checkedClean}
            onClick={() => {
              setDialog("publish");
            }}
          >
            Publish version {ruleSet.publish_counter + 1}
          </Button>
        ) : null}
        {canEdit ? (
          <Button
            variant="ghost"
            size="small"
            icon={FileJson}
            onClick={openJson}
          >
            Edit draft as JSON
          </Button>
        ) : null}
        {canEdit && ruleSet.publish_counter === 0 ? (
          <Button
            variant="ghost"
            size="small"
            icon={Trash2}
            onClick={() => {
              setDialog("delete-set");
            }}
          >
            Delete rule set
          </Button>
        ) : null}
        {canPublish && !checkedClean ? (
          <span className={styles.muted}>
            Run Check draft; publishing needs a clean check of this revision.
          </span>
        ) : null}
      </div>

      <div className={styles.editor}>
        <nav aria-label="Rules in the draft" className={styles.tight}>
          {rules.length === 0 ? (
            <p className={styles.muted}>The draft has no rules yet.</p>
          ) : (
            <ul className={styles.ruleList}>
              {rules.map((rule) => {
                const status = statusOf(report, rule.key);
                return (
                  <li key={rule.key} className={styles.ruleItem}>
                    <button
                      type="button"
                      className={styles.ruleButton}
                      aria-current={selected === rule.key ? "true" : undefined}
                      onClick={() => {
                        select(rule.key);
                      }}
                    >
                      <span className={styles.ruleKey}>{rule.key}</span>
                      <span className={styles.ruleMeta}>
                        <Badge intent={severityIntent[rule.severity]}>
                          {rule.severity}
                        </Badge>
                        {status === "ok" ? (
                          <span className={styles.ok}>
                            <CircleCheck size={14} aria-hidden="true" /> Check
                            passed
                          </span>
                        ) : status === "problems" ? (
                          <span className={styles.bad}>
                            <CircleX size={14} aria-hidden="true" /> Has
                            problems
                          </span>
                        ) : (
                          <span>
                            <CircleDashed size={14} aria-hidden="true" /> Not
                            checked
                          </span>
                        )}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {canEdit ? (
            <Button
              variant="secondary"
              size="small"
              icon={Plus}
              onClick={() => {
                select(NEW_RULE);
              }}
            >
              Add rule
            </Button>
          ) : null}
        </nav>

        <section
          aria-label={
            selected === NEW_RULE ? "New rule" : `Rule ${selected ?? ""}`
          }
          className={styles.stack}
        >
          {values === null ? (
            <EmptyState
              level={3}
              heading="No rule selected"
              body={
                canEdit
                  ? "Add a rule: a JSONPath target, a CEL condition that is true when the contract complies, and pass and fail fixtures that prove it."
                  : "This draft has no rules."
              }
            />
          ) : (
            <>
              <RuleForm
                key={selected ?? "none"}
                values={values}
                onChange={setValues}
                errors={errors}
                problems={selectedProblems}
                checked={report?.status === "ok" && selected !== NEW_RULE}
                readOnly={!canEdit}
                keyLocked={selected !== NEW_RULE}
              />
              {canEdit ? (
                <div className={styles.actions}>
                  <Button
                    variant="primary"
                    size="small"
                    busy={writes.putRule.isPending}
                    busyLabel="Saving…"
                    onClick={saveRule}
                  >
                    {selected === NEW_RULE ? "Add rule to draft" : "Save rule"}
                  </Button>
                  {selected !== NEW_RULE && selected !== null ? (
                    <Button
                      variant="ghost"
                      size="small"
                      icon={Trash2}
                      onClick={() => {
                        setDialog("delete-rule");
                      }}
                    >
                      Remove rule
                    </Button>
                  ) : null}
                </div>
              ) : null}
            </>
          )}
        </section>
      </div>

      <ConfirmDialog
        open={dialog === "delete-rule"}
        onOpenChange={(open) => {
          setDialog(open ? "delete-rule" : null);
        }}
        title={`Remove rule ${selected ?? ""}?`}
        body="The rule leaves the draft. Published versions keep it."
        objectName="rule"
        confirmLabel="Remove rule"
        busyLabel="Removing…"
        onConfirm={async () => {
          if (selected === null || selected === NEW_RULE) return;
          const next = await writes.deleteRule.mutateAsync({
            revision,
            key: selected,
          });
          afterWrite(next);
          const first = [...next.rules].sort((a, b) =>
            a.key.localeCompare(b.key),
          )[0];
          setSelected(first?.key ?? null);
          setValues(first ? formFromRule(first) : null);
        }}
      />
      <ConfirmDialog
        open={dialog === "publish"}
        onOpenChange={(open) => {
          setDialog(open ? "publish" : null);
        }}
        title={`Publish version ${String(ruleSet.publish_counter + 1)} of ${ruleSet.name}?`}
        body={
          <>
            Contracts that attach it with &quot;latest&quot; are validated
            against it from now on.
            {ruleSet.mandatory
              ? " It is mandatory: it warns for the grace period and then blocks publishes."
              : ""}
          </>
        }
        dismissLabel="Go back"
        confirmLabel="Publish rule set"
        busyLabel="Publishing…"
        onConfirm={async () => {
          const version = await writes.publish.mutateAsync(revision);
          setNotice(`Version ${String(version.version_no)} is published.`);
        }}
      />
      <ConfirmDialog
        open={dialog === "delete-set"}
        onOpenChange={(open) => {
          setDialog(open ? "delete-set" : null);
        }}
        title={`Delete ${ruleSet.name}?`}
        body="The rule set and its draft are deleted. It was never published, so no contract depends on it."
        objectName={ruleSet.name}
        confirmLabel="Delete rule set"
        typeToConfirm={ruleSet.name}
        busyLabel="Deleting…"
        onConfirm={async () => {
          await writes.remove.mutateAsync();
          void navigate({
            to: "/rule-sets",
            search: ruleSet.folder_id ? { folder_id: ruleSet.folder_id } : {},
          });
        }}
      />
      <Dialog
        open={dialog === "json"}
        onOpenChange={(open) => {
          setDialog(open ? "json" : null);
        }}
        size={896}
        title="Edit draft as JSON"
        description="Replaces every rule in the draft at once. Use it to import rules or edit the description."
        footer={
          <>
            <Button
              variant="secondary"
              size="small"
              onClick={() => {
                setDialog(null);
              }}
            >
              Go back
            </Button>
            <Button
              variant="primary"
              size="small"
              busy={writes.putDraft.isPending}
              busyLabel="Saving…"
              onClick={saveJson}
            >
              Replace draft
            </Button>
          </>
        }
      >
        <TextareaField
          id="rule-set-json"
          label="Draft (JSON)"
          monospace
          rows={22}
          spellCheck={false}
          value={jsonText}
          error={jsonError}
          onChange={(event) => {
            setJsonText(event.target.value);
          }}
        />
      </Dialog>
    </div>
  );
}
