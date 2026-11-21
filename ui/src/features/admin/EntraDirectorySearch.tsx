// Search the Entra directory for the object to register (quick 261119-ofa, 261119-r5g). A debounced
// combobox over GET /api/v1/identity/entra/directory/search: the matches drop down under the input
// while typing (display name, then the secondary text and the object ID in the normal font). Options
// that are already registered are listed but disabled. Picking one puts its display name in the box
// and its secondary text and object ID in the helper line under it. A 503 (directory unreachable or Entra not configured) shows a warning with
// a retry; the dialog keeps its "Enter an object ID instead" path either way. Registration itself
// stays the unchanged POST /entra/{users,groups,service-principals}.
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { EntraDirectoryMatch } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import {
  Combobox,
  type ComboboxOption,
  type ComboboxStatus,
} from "@/components/Combobox";
import { problemOf } from "@/lib/unexpected";
import { idKeys, useApi } from "./shared";
import styles from "./admin.module.css";

export type DirectoryKind = "user" | "group" | "service-principal";

const MIN_CHARS = 2;
const DEBOUNCE_MS = 300;
const GUID_OK =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const COPY: Record<DirectoryKind, { label: string; helper: string }> = {
  user: {
    label: "Search Entra users",
    helper:
      "Name, email or user principal name (at least 2 characters), or paste an object ID.",
  },
  group: {
    label: "Search Entra groups",
    helper: "Name or email (at least 2 characters), or paste an object ID.",
  },
  "service-principal": {
    label: "Search Entra service principals",
    helper:
      "Name or application (client) ID (at least 2 characters), or paste an object ID.",
  },
};

const API_KIND = {
  user: "user",
  group: "group",
  "service-principal": "service_principal",
} as const;

/** The same stripping the server applies: no quotes, backslashes or control characters. */
export function searchTerm(raw: string): string {
  let kept = "";
  for (const ch of raw) {
    if (ch === '"' || ch === "\\") continue;
    if (/\s/u.test(ch)) kept += " ";
    else if (!/\p{C}/u.test(ch)) kept += ch;
  }
  return kept.replace(/\s+/g, " ").trim().slice(0, 100).trim();
}

export function EntraDirectorySearch({
  kind,
  id,
  ownerId,
  tenantId,
  picked,
  onPick,
  error,
}: {
  kind: DirectoryKind;
  /** Prefix for the element ids. */
  id: string;
  /** The chosen owner folder; required for groups and service principals. */
  ownerId: string;
  /** A valid lower-case tenant GUID, or empty for the configured tenant. */
  tenantId: string;
  picked: EntraDirectoryMatch | null;
  onPick: (match: EntraDirectoryMatch | null) => void;
  error?: string;
}) {
  const api = useApi();
  const [typed, setTyped] = useState("");
  const [settled, setSettled] = useState("");
  const copy = COPY[kind];
  const term = searchTerm(typed);
  // The box holds the picked name: that is not a new search.
  const showingPick = picked !== null && typed === picked.display_name;

  // Debounce with a local effect: one request per pause in typing, never one per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      setSettled(term);
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [term]);

  const needsOwner = kind !== "user";
  const enabled =
    !showingPick &&
    settled.length >= MIN_CHARS &&
    (!needsOwner || ownerId !== "");
  const tenant = GUID_OK.test(tenantId) ? tenantId : undefined;
  const search = useQuery({
    queryKey: [
      ...idKeys.entraDirectory,
      API_KIND[kind],
      needsOwner ? ownerId : null,
      tenant ?? null,
      settled,
    ],
    enabled,
    queryFn: async () => {
      const { data } = await api.GET(
        "/api/v1/identity/entra/directory/search",
        {
          params: {
            query: {
              kind: API_KIND[kind],
              q: settled,
              tenant_id: tenant,
              owner_folder_id: needsOwner ? ownerId : undefined,
            },
          },
        },
      );
      return data ?? { items: [], truncated: false };
    },
  });

  const waiting = term.length >= MIN_CHARS && term !== settled && !showingPick;
  const unreachable =
    search.isError &&
    (() => {
      const problem = problemOf(search.error);
      return problem.kind === "problem" && problem.status === 503;
    })();
  const items = enabled ? (search.data?.items ?? []) : [];
  const secondaryOf = (match: EntraDirectoryMatch) =>
    match.secondary
      ? kind === "service-principal"
        ? `Application ID ${match.secondary}`
        : match.secondary
      : undefined;
  const options: ComboboxOption[] =
    enabled && search.isSuccess && !waiting
      ? items.map((match) => ({
          key: match.object_id,
          label: match.display_name,
          detail: secondaryOf(match),
          meta: match.object_id,
          note: match.already_registered ? "Already registered" : undefined,
          disabled: match.already_registered,
        }))
      : [];

  let status: ComboboxStatus | null = null;
  if (waiting || (enabled && search.isFetching))
    status = { text: "Searching…", busy: true };
  else if (enabled && search.isSuccess) {
    if (items.length === 0)
      status = {
        text: "No matches in the directory. Check the spelling, or enter an object ID instead.",
      };
    else if (search.data.truncated)
      status = {
        text: "Showing the first 20 matches. Type more to narrow the list.",
      };
  }

  const pickedLine = picked
    ? [secondaryOf(picked), picked.object_id].filter(Boolean).join(" · ")
    : undefined;

  return (
    <div className={styles.form}>
      <Combobox
        id={`${id}-search`}
        label={copy.label}
        listLabel="Directory matches"
        maxLength={100}
        value={typed}
        helper={pickedLine ?? copy.helper}
        error={error}
        options={options}
        selectedKey={picked?.object_id ?? null}
        status={status}
        onValueChange={(next) => {
          setTyped(next);
          if (picked) onPick(null);
        }}
        onSelect={(option) => {
          const match = items.find((m) => m.object_id === option.key);
          if (!match) return;
          setTyped(match.display_name);
          onPick(match);
        }}
      />
      {needsOwner && ownerId === "" && term.length >= MIN_CHARS ? (
        <p className={styles.muted}>Choose an owner to search.</p>
      ) : null}
      {unreachable ? (
        <Alert
          intent="warning"
          action={
            <Button
              variant="secondary"
              size="small"
              onClick={() => {
                void search.refetch();
              }}
            >
              Retry search
            </Button>
          }
        >
          The Entra directory is unreachable or not configured, so it cannot be
          searched. Retry, or enter an object ID instead.
        </Alert>
      ) : search.isError && enabled ? (
        <Alert intent="danger">
          The directory search failed. Retry, or enter an object ID instead.
        </Alert>
      ) : null}
    </div>
  );
}
