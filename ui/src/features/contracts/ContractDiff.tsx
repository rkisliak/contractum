// Diff tab (D-UI-10-13): pick two refs (published versions, Unpublished ones for publishers, the
// Latest alias and drafts for readers), swap them, then GET .../diff. The change list is grouped
// Breaking, Warning, Info with counts and the required bump; "Load more" follows next_cursor. A
// side-by-side view compares both documents fetched as JSON. The server classifies; the UI never
// does.
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeftRight } from "lucide-react";
import type { ApiClient } from "@/api/client";
import { Alert } from "@/components/Alert";
import { Badge, type BadgeIntent } from "@/components/Badge";
import { Button } from "@/components/Button";
import { SelectField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState, Skeleton } from "@/components/states";
import { useCapabilities } from "@/features/access/useCapabilities";
import { DiffView } from "@/features/editor/CodeEditor";
import { problemOf } from "@/lib/unexpected";
import {
  contractKeys,
  fetchDraftDocument,
  fetchVersionDocument,
  useApi,
  useContract,
  useContractVersions,
  useDrafts,
} from "./api";
import type { CompatibilityChange } from "./report";
import styles from "./Contracts.module.css";

export type DiffSearch = { from?: string; to?: string };

const CLASSES = [
  { key: "breaking", label: "Breaking", intent: "danger" },
  { key: "warning", label: "Warning", intent: "warning" },
  { key: "info", label: "Info", intent: "neutral" },
] as const satisfies readonly {
  key: string;
  label: string;
  intent: BadgeIntent;
}[];

export function groupChanges(changes: CompatibilityChange[]) {
  return CLASSES.map((klass) => ({
    ...klass,
    changes: changes.filter((change) => change.class === klass.key),
  }));
}

export function changeTitle(change: CompatibilityChange): string {
  const words = change.code.replace(/-/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function show(value: unknown): string {
  if (value === null || value === undefined) return "not set";
  return typeof value === "string" ? value : JSON.stringify(value);
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

async function fetchSide(api: ApiClient, contractId: string, ref: string) {
  const doc = UUID.test(ref)
    ? await fetchDraftDocument(api, contractId, ref, "json")
    : await fetchVersionDocument(api, contractId, ref, "json");
  try {
    return JSON.stringify(JSON.parse(doc.text), null, 2);
  } catch {
    return doc.text;
  }
}

export function ContractDiff({
  contractId,
  search,
  onSearchChange,
}: {
  contractId: string;
  search: DiffSearch;
  onSearchChange: (next: DiffSearch) => void;
}) {
  const api = useApi();
  const versions = useContractVersions(contractId);
  // Drafts are offered only to a caller who may read them (contract:read).
  const contract = useContract(contractId);
  const caps = useCapabilities(contract.data?.folder_id ?? null, {
    enabled: contract.data !== undefined,
  });
  const drafts = useDrafts(contractId, caps.can("contract:read"));
  const options = [
    { value: "latest", label: "Latest" },
    ...(versions.data ?? []).map((version) => ({
      value: version.semver,
      label: `${version.semver}${version.status === "latest" ? " (Latest)" : version.status === "preview" ? " (Preview)" : ""}${version.lifecycle === "unpublished" ? " (Unpublished)" : ""}`,
    })),
    ...(drafts.data ?? []).map((draft) => ({
      value: draft.id,
      label: `Draft ${draft.declared_version ?? ""} (revision ${String(draft.revision)})`,
    })),
  ];
  const from = search.from ?? "";
  const to = search.to ?? "";
  const ready = from !== "" && to !== "" && from !== to;

  const diff = useInfiniteQuery({
    queryKey: [...contractKeys.all(contractId), "diff", from, to],
    enabled: ready,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}/diff",
        {
          params: {
            path: { contract_id: contractId },
            query: {
              from,
              to,
              limit: 500,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        },
      );
      if (!data) throw new Error("diff had no body");
      return data;
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: false,
  });
  const sides = useQuery({
    queryKey: [...contractKeys.all(contractId), "diff-docs", from, to],
    enabled: ready,
    queryFn: async () => {
      const [left, right] = await Promise.all([
        fetchSide(api, contractId, from),
        fetchSide(api, contractId, to),
      ]);
      return { left, right };
    },
    retry: false,
  });

  const first = diff.data?.pages[0];
  const changes = (diff.data?.pages.flatMap((page) => page.changes) ??
    []) as unknown as CompatibilityChange[];

  return (
    <section className={styles.stack} aria-label="Diff">
      <form
        className={styles.toolbar}
        onSubmit={(event) => {
          event.preventDefault();
        }}
      >
        <div className={styles.toolbarField}>
          <SelectField
            id="diff-from"
            label="From (older)"
            placeholder="Choose a version"
            options={options}
            value={from}
            onChange={(event) => {
              onSearchChange({
                ...search,
                from: event.target.value || undefined,
              });
            }}
          />
        </div>
        <Button
          type="button"
          variant="outline-neutral"
          size="small"
          icon={ArrowLeftRight}
          onClick={() => {
            onSearchChange({ from: search.to, to: search.from });
          }}
        >
          Swap
        </Button>
        <div className={styles.toolbarField}>
          <SelectField
            id="diff-to"
            label="To (newer)"
            placeholder="Choose a version"
            options={options}
            value={to}
            onChange={(event) => {
              onSearchChange({
                ...search,
                to: event.target.value || undefined,
              });
            }}
          />
        </div>
      </form>
      {!ready ? (
        <EmptyState
          heading="Pick two versions to compare"
          body="Choose an older and a newer version, the Latest or a draft. The platform classifies every change as breaking, warning or info."
        />
      ) : diff.error ? (
        <ProblemAlert
          problem={problemOf(diff.error)}
          onRetry={() => void diff.refetch()}
        />
      ) : !first ? (
        <Skeleton label="comparison" rows={4} />
      ) : (
        <>
          <div className={styles.toolbar}>
            <Badge
              intent={
                first.required_bump === "major"
                  ? "danger"
                  : first.required_bump === "minor"
                    ? "warning"
                    : "neutral"
              }
            >
              Required bump: {first.required_bump}
            </Badge>
            {CLASSES.map((klass) => (
              <Badge key={klass.key} intent={klass.intent}>
                {klass.label}: {String(first.counts[klass.key] ?? 0)}
              </Badge>
            ))}
            <span className={styles.muted}>
              {first.changes_total} changes in total
            </span>
          </div>
          {first.truncated ? (
            <Alert intent="info">Showing the first 10,000 changes.</Alert>
          ) : null}
          {changes.length === 0 ? (
            <p className={styles.muted}>
              The two documents are equivalent: no changes.
            </p>
          ) : (
            groupChanges(changes).map((group) =>
              group.changes.length === 0 ? null : (
                <div key={group.key} className={styles.stackTight}>
                  <h2 className={styles.sectionHeading}>
                    {group.label} ({group.changes.length})
                  </h2>
                  <ul className={styles.changeList}>
                    {group.changes.map((change, index) => (
                      <li
                        key={`${change.code}-${change.path}-${String(index)}`}
                        className={styles.changeItem}
                      >
                        <span className={styles.badges}>
                          <Badge intent={group.intent}>{group.label}</Badge>
                          <strong>{changeTitle(change)}</strong>
                        </span>
                        <code className={styles.mono}>
                          {change.path || "(contract)"}
                          {change.attribute ? ` · ${change.attribute}` : ""}
                        </code>
                        {change.before !== null || change.after !== null ? (
                          <span>
                            {show(change.before)} → {show(change.after)}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </div>
              ),
            )
          )}
          {diff.hasNextPage ? (
            <div>
              <Button
                variant="secondary"
                size="small"
                busy={diff.isFetchingNextPage}
                busyLabel="Loading…"
                onClick={() => void diff.fetchNextPage()}
              >
                Load more changes
              </Button>
            </div>
          ) : null}
          <h2 className={styles.sectionHeading}>Side by side (JSON)</h2>
          {sides.error ? (
            <ProblemAlert problem={problemOf(sides.error)} />
          ) : sides.data ? (
            <DiffView
              original={sides.data.left}
              modified={sides.data.right}
              language="json"
              fit="fill"
            />
          ) : (
            <Skeleton label="documents" rows={6} />
          )}
        </>
      )}
    </section>
  );
}
