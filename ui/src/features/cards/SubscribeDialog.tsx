// Subscribe a card (D-UI-13-03): search a contract, pick one of its *published* versions (a pin is
// a concrete SemVer, never `latest`), then POST .../subscriptions. Moving a pin reuses the version
// picker for the same contract and confirms "Move the pin from A to B?".
import { useQuery } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import type { ContractSummary } from "@/api/types";
import { Button } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { SelectField, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Spinner } from "@/components/states";
import { cx } from "@/lib/cx";
import { problemOf } from "@/lib/unexpected";
import { useApi, useCardWrites, usePublishedVersions } from "./api";
import styles from "./Cards.module.css";

function useContractSearch(q: string) {
  const api = useApi();
  return useQuery({
    queryKey: ["contracts", "search", q],
    queryFn: async (): Promise<ContractSummary[]> => {
      const { data } = await api.GET("/api/v1/contracts/contracts", {
        params: { query: { limit: 20, ...(q ? { q } : {}) } },
      });
      if (data === undefined) throw new Error("GET /contracts had no body");
      return data.items;
    },
  });
}

export function VersionSelect({
  id,
  contractId,
  value,
  onChange,
  exclude,
  error,
}: {
  id: string;
  contractId: string | null;
  value: string;
  onChange: (value: string) => void;
  exclude?: string;
  error?: string;
}) {
  const versions = usePublishedVersions(contractId);
  if (contractId === null) return null;
  if (versions.isPending) return <Spinner label="Loading versions" />;
  if (versions.error)
    return <ProblemAlert problem={problemOf(versions.error)} />;
  const options = versions.data
    .filter((version) => version.semver !== exclude)
    .map((version) => ({
      value: version.semver,
      label: `${version.semver}${version.status === "latest" ? " (latest)" : version.status === "preview" ? " (preview)" : ""}${version.deprecated ? " · deprecated" : ""}`,
    }));
  return (
    <SelectField
      id={id}
      label="Version"
      helper={
        options.length === 0
          ? "This contract has no other published version to pin."
          : "A pin is always one published version."
      }
      options={options}
      placeholder="Choose a version"
      value={value}
      error={error}
      disabled={options.length === 0}
      onChange={(event) => {
        onChange(event.target.value);
      }}
    />
  );
}

export function SubscribeDialog({
  open,
  onOpenChange,
  cardId,
  cardName,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  cardId: string;
  cardName: string;
}) {
  const { subscribe } = useCardWrites();
  const [query, setQuery] = useState("");
  const [contract, setContract] = useState<ContractSummary | null>(null);
  const [version, setVersion] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);
  const results = useContractSearch(query.trim());

  function reset() {
    setQuery("");
    setContract(null);
    setVersion("");
    setError(undefined);
    setProblem(null);
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    if (contract === null) {
      setError("Choose a contract first.");
      return;
    }
    if (version === "") {
      setError("Choose a published version to pin.");
      return;
    }
    setError(undefined);
    subscribe.mutate(
      { cardId, contractId: contract.id, version },
      {
        onSuccess: () => {
          reset();
          onOpenChange(false);
        },
        onError: (failure) => {
          setProblem(problemOf(failure));
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      size={512}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Subscribe to a contract"
      description={`Pin a published contract version to ${cardName}. The card is notified when that version changes.`}
      footer={
        <>
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              reset();
              onOpenChange(false);
            }}
          >
            Go back
          </Button>
          <Button
            variant="primary"
            size="small"
            type="submit"
            form="subscribe-form"
            busy={subscribe.isPending}
            busyLabel="Subscribing…"
          >
            Subscribe card
          </Button>
        </>
      }
    >
      <form
        id="subscribe-form"
        className={styles.form}
        noValidate
        onSubmit={onSubmit}
      >
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <TextField
          id="subscribe-search"
          label="Find a contract"
          type="search"
          autoComplete="off"
          maxLength={200}
          value={query}
          error={contract === null ? error : undefined}
          onChange={(event) => {
            setQuery(event.target.value);
          }}
        />
        {results.error ? (
          <ProblemAlert problem={problemOf(results.error)} />
        ) : results.isPending ? (
          <Spinner label="Searching contracts" />
        ) : results.data.length === 0 ? (
          <p className={styles.muted}>No contracts match.</p>
        ) : (
          <ul className={styles.results} aria-label="Matching contracts">
            {results.data.map((item) => {
              const selected = contract?.id === item.id;
              return (
                <li key={item.id}>
                  <button
                    type="button"
                    className={cx(
                      styles.result,
                      selected && styles.resultSelected,
                    )}
                    aria-pressed={selected}
                    onClick={() => {
                      setContract(item);
                      setVersion(item.latest_version?.semver ?? "");
                      setError(undefined);
                    }}
                  >
                    {item.name ?? item.odcs_id}
                    <span className={styles.secondary}>
                      {item.odcs_id}
                      {item.latest_version
                        ? ` · latest ${item.latest_version.semver}`
                        : " · not published"}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        <VersionSelect
          id="subscribe-version"
          contractId={contract?.id ?? null}
          value={version}
          error={contract !== null ? error : undefined}
          onChange={(next) => {
            setVersion(next);
            setError(undefined);
          }}
        />
      </form>
    </Dialog>
  );
}

export function MoveDialog({
  open,
  onOpenChange,
  cardId,
  subscriptionId,
  contractId,
  from,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  cardId: string;
  subscriptionId: string;
  contractId: string;
  from: string;
}) {
  const { move } = useCardWrites();
  const [version, setVersion] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);

  function reset() {
    setVersion("");
    setError(undefined);
    setProblem(null);
  }

  return (
    <Dialog
      open={open}
      size={448}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Move pin"
      description={
        version
          ? `Move the pin from ${from} to ${version}?`
          : `The card is pinned to ${from}. Choose the version to move to.`
      }
      footer={
        <>
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              reset();
              onOpenChange(false);
            }}
          >
            Keep {from}
          </Button>
          <Button
            variant="primary"
            size="small"
            type="submit"
            form="move-form"
            busy={move.isPending}
            busyLabel="Moving…"
          >
            Move pin
          </Button>
        </>
      }
    >
      <form
        id="move-form"
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (version === "") {
            setError("Choose the version to move to.");
            return;
          }
          move.mutate(
            { cardId, subscriptionId, version },
            {
              onSuccess: () => {
                reset();
                onOpenChange(false);
              },
              onError: (failure) => {
                setProblem(problemOf(failure));
              },
            },
          );
        }}
      >
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <VersionSelect
          id="move-version"
          contractId={open ? contractId : null}
          value={version}
          exclude={from}
          error={error}
          onChange={(next) => {
            setVersion(next);
            setError(undefined);
          }}
        />
      </form>
    </Dialog>
  );
}
