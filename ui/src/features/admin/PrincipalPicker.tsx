// The shared principal picker (D-UI-12-08, quick 261119-l30, 261119-r5g): a type select and a
// searchable dropdown (Combobox) over the matching list endpoints. It is search-only: until two characters are typed it
// shows a hint and requests nothing, then it matches display name, username, email, object ID and
// record id, shows at most 20 matches and asks the user to type more to narrow the rest. A pasted
// full object ID finds exactly that item. It shows display names and builds `principal_id` in the
// documented shape, so nobody types `local:<uuid>`. Deactivated principals are listed but disabled.
// Listing users needs the root-level `user:read`: without it the user lists are never requested; where
// the API accepts it (`allowUsername`: grants, group members) an Org admin can still name a local
// user by exact username through the "Use local username" result. There are no free-text fields:
// an unregistered Entra group is registered first, on Groups > New > Register Entra group.
import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Alert } from "@/components/Alert";
import {
  Combobox,
  type ComboboxOption,
  type ComboboxStatus,
} from "@/components/Combobox";
import { SelectField } from "@/components/form";
import { hasGlobal, useMe } from "@/features/access/useMe";
import { cx } from "@/lib/cx";
import { fetchAllPages, MAX_PAGES, PAGE_LIMIT } from "@/lib/fetchAllPages";
import {
  PRINCIPAL_TYPE_LABELS,
  principalIdOf,
  type PrincipalType,
} from "./principal";
import { useApi } from "./shared";
import styles from "./admin.module.css";

export type PickedPrincipal = {
  type: PrincipalType;
  /** Empty when the principal is a local user named by `username`. */
  id: string;
  label: string;
  /** A local user named by exact username; the server resolves it (unknown-principal if not). */
  username?: string;
};

type Candidate = PickedPrincipal & {
  hint: string;
  disabled: boolean;
  /** Lower-cased texts the search matches: names, username, email, object ID and record id. */
  terms: string[];
};

const terms = (...values: (string | null | undefined)[]): string[] =>
  values.flatMap((value) =>
    value === null || value === undefined || value === ""
      ? []
      : [value.toLowerCase()],
  );

/** Fewer characters than this request nothing: the lists have no server search. */
const MIN_CHARS = 2;
const HINT = "Type at least 2 characters: name, username, email or object ID";
const NARROW = "Type more to narrow the list";
const USERNAME_KEY = "__local-username__";
const USERNAME_NOTE = "The server checks that this username exists";

/** What a typed local username may look like (the server decides whether it exists). */
const USERNAME_LIKE = /^[A-Za-z0-9._@-]+$/;

const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** At most this many matches are rendered; the search narrows the rest. */
const SHOWN_MAX = 20;

type CandidateList = {
  items: Candidate[];
  /** A directory could not be listed (the other one could). */
  partial: boolean;
  /** A list was longer than the page cap; only the first records are offered. */
  truncated: boolean;
};

// The list endpoints have no server search, so each list is read to its end (fetchAllPages) and
// filtered here; a record past the first page is found like any other.
function useCandidates(
  type: PrincipalType,
  folderId: string | undefined,
  enabled: boolean,
) {
  const api = useApi();
  return useQuery({
    queryKey: ["identity", "picker", type, folderId ?? null],
    enabled,
    queryFn: async (): Promise<CandidateList> => {
      switch (type) {
        case "user": {
          const [local, entra] = await Promise.allSettled([
            fetchAllPages(async (query) => {
              const { data } = await api.GET("/api/v1/identity/users", {
                params: { query },
              });
              return data;
            }),
            fetchAllPages(async (query) => {
              const { data } = await api.GET("/api/v1/identity/entra/users", {
                params: { query },
              });
              return data;
            }),
          ]);
          const items: Candidate[] = [];
          let truncated = false;
          if (local.status === "fulfilled") {
            truncated ||= local.value.truncated;
            for (const u of local.value.items)
              items.push({
                type,
                id: principalIdOf.localUser(u.id),
                label: u.display_name,
                hint: `${u.username} · Local`,
                disabled: !u.is_active,
                terms: terms(u.display_name, u.username, u.email, u.id),
              });
          }
          if (entra.status === "fulfilled") {
            truncated ||= entra.value.truncated;
            for (const u of entra.value.items)
              items.push({
                type,
                id: u.principal_id,
                label: u.display_name,
                hint: `${u.preferred_username ?? u.object_id} · Entra`,
                disabled: !u.is_active,
                terms: terms(
                  u.display_name,
                  u.preferred_username,
                  u.object_id,
                  u.id,
                ),
              });
          }
          if (local.status === "rejected" && entra.status === "rejected")
            throw local.reason;
          return {
            items,
            partial: local.status === "rejected" || entra.status === "rejected",
            truncated,
          };
        }
        case "local_group": {
          if (folderId) {
            const { data } = await api.GET(
              "/api/v1/identity/folders/{folder_id}/grantable-groups",
              { params: { path: { folder_id: folderId } } },
            );
            return {
              items: (data?.items ?? []).map((g) => ({
                type,
                id: principalIdOf.localGroup(g.id),
                label: g.name,
                hint: "Local group",
                disabled: false,
                terms: terms(g.name, g.id),
              })),
              partial: false,
              truncated: false,
            };
          }
          const all = await fetchAllPages(async (query) => {
            const { data } = await api.GET("/api/v1/identity/groups", {
              params: { query },
            });
            return data;
          });
          return {
            items: all.items.map((g) => ({
              type,
              id: principalIdOf.localGroup(g.id),
              label: g.name,
              hint: "Local group",
              disabled: false,
              terms: terms(g.name, g.id),
            })),
            partial: false,
            truncated: all.truncated,
          };
        }
        case "entra_group": {
          const all = await fetchAllPages(async (query) => {
            const { data } = await api.GET("/api/v1/identity/entra/groups", {
              params: { query },
            });
            return data;
          });
          return {
            items: all.items.map((g) => ({
              type,
              id: principalIdOf.entraGroup(g.object_id),
              label: g.display_name,
              hint: g.object_id,
              disabled: false,
              terms: terms(g.display_name, g.object_id, g.id),
            })),
            partial: false,
            truncated: all.truncated,
          };
        }
        case "service_account": {
          const all = await fetchAllPages(async (query) => {
            const { data } = await api.GET(
              "/api/v1/identity/service-accounts",
              { params: { query } },
            );
            return data;
          });
          return {
            items: all.items.map((a) => ({
              type,
              id: a.principal_id,
              label: a.name,
              hint: "Service account",
              disabled: !a.is_active,
              terms: terms(a.name, a.id),
            })),
            partial: false,
            truncated: all.truncated,
          };
        }
        case "service_principal": {
          const all = await fetchAllPages(async (query) => {
            const { data } = await api.GET(
              "/api/v1/identity/entra/service-principals",
              { params: { query } },
            );
            return data;
          });
          return {
            items: all.items.map((sp) => ({
              type,
              id: sp.principal_id,
              label: sp.display_name,
              hint: sp.object_id,
              disabled: !sp.is_active,
              terms: terms(sp.display_name, sp.object_id, sp.id),
            })),
            partial: false,
            truncated: all.truncated,
          };
        }
      }
    },
  });
}

export function PrincipalPicker({
  types,
  folderId,
  value,
  onChange,
  error,
  allowUsername = false,
}: {
  types: PrincipalType[];
  /** Granting at a folder: local groups come from that folder's grantable groups. */
  folderId?: string;
  value: PickedPrincipal | null;
  onChange: (value: PickedPrincipal | null) => void;
  error?: string;
  /** Offer "Use local username" for a caller who cannot list users (the API accepts `username`). */
  allowUsername?: boolean;
}) {
  const ids = useId();
  const me = useMe();
  const [type, setType] = useState<PrincipalType>(types[0] ?? "user");
  const [search, setSearch] = useState("");
  const typed = search.trim();
  const needle = typed.toLowerCase();
  // Both user lists need the root-level user:read: without it they are never requested.
  const canList =
    type !== "user" ||
    (me.data !== undefined && hasGlobal(me.data, "user:read"));
  const searching = needle.length >= MIN_CHARS;
  const candidates = useCandidates(type, folderId, searching && canList);
  // "users", "local groups", "Entra groups": Entra stays a proper noun.
  const typeName = PRINCIPAL_TYPE_LABELS[type];
  const typeLabel = typeName.startsWith("Entra")
    ? typeName.charAt(0) + typeName.slice(1).toLowerCase()
    : typeName.toLowerCase();
  const offerUsername =
    allowUsername &&
    type === "user" &&
    me.data !== undefined &&
    !canList &&
    searching &&
    USERNAME_LIKE.test(typed);
  // A pasted full object ID finds exactly that item; anything else matches as a substring.
  const matches = !searching
    ? []
    : (candidates.data?.items ?? []).filter((c) =>
        GUID.test(needle)
          ? c.terms.includes(needle)
          : c.terms.some((term) => term.includes(needle)),
      );
  const shown = matches.slice(0, SHOWN_MAX);

  const pickedCandidate = value
    ? (candidates.data?.items ?? []).find(
        (c) => c.id === value.id && c.type === value.type,
      )
    : undefined;
  const pickedHint =
    value === null
      ? undefined
      : value.username !== undefined
        ? USERNAME_NOTE
        : pickedCandidate?.hint;
  const options: ComboboxOption[] = [
    ...(offerUsername
      ? [
          {
            key: USERNAME_KEY,
            label: `Use local username “${typed}”`,
            detail: USERNAME_NOTE,
          },
        ]
      : []),
    ...shown.map((c) => ({
      key: c.id,
      label: c.disabled ? `${c.label} (deactivated)` : c.label,
      detail: c.hint,
      disabled: c.disabled,
    })),
  ];
  const loaded = canList && candidates.isSuccess;
  let status: ComboboxStatus | null = null;
  if (searching && canList && candidates.isPending)
    status = { text: `Loading ${typeLabel}s…`, busy: true };
  else if (searching && (loaded || offerUsername)) {
    if (options.length === 0) status = { text: `No ${typeLabel}s match.` };
    else if (matches.length > shown.length)
      status = {
        text: `Showing ${String(shown.length)} of ${String(matches.length)} matches. ${NARROW}.`,
      };
  }

  return (
    <fieldset className={cx(styles.form, styles.fieldset)}>
      <legend className={styles.sectionHeading}>Principal</legend>
      {types.length > 1 ? (
        <SelectField
          id={`${ids}-type`}
          label="Principal type"
          value={type}
          options={types.map((t) => ({
            value: t,
            label: PRINCIPAL_TYPE_LABELS[t],
          }))}
          onChange={(event) => {
            setType(event.target.value as PrincipalType);
            setSearch("");
            onChange(null);
          }}
        />
      ) : null}
      <Combobox
        id={`${ids}-search`}
        label={`Search ${typeLabel}s`}
        listLabel={`${PRINCIPAL_TYPE_LABELS[type]} results`}
        controlSize="small"
        value={search}
        helper={value ? pickedHint : HINT}
        options={options}
        selectedKey={
          value === null
            ? null
            : value.username !== undefined
              ? USERNAME_KEY
              : value.id
        }
        status={status}
        onValueChange={(next) => {
          setSearch(next);
          if (value) onChange(null);
        }}
        onSelect={(option) => {
          if (option.key === USERNAME_KEY) {
            setSearch(typed);
            onChange({ type, id: "", label: typed, username: typed });
            return;
          }
          const c = shown.find((item) => item.id === option.key);
          if (!c) return;
          setSearch(c.label);
          onChange({ type: c.type, id: c.id, label: c.label });
        }}
      />
      {searching && !canList && !offerUsername ? (
        <p className={styles.muted}>
          The list of {typeLabel}s is not available at your access level.
        </p>
      ) : null}
      {searching && canList && candidates.isError ? (
        <Alert intent="warning">
          The {typeLabel} list could not be loaded.
        </Alert>
      ) : null}
      {searching && canList && candidates.data?.partial ? (
        <p className={styles.muted}>Some directories could not be listed.</p>
      ) : null}
      {searching && canList && candidates.data?.truncated ? (
        <p className={styles.muted}>
          Only the first {MAX_PAGES * PAGE_LIMIT} {typeLabel}s could be loaded.
        </p>
      ) : null}
      {error ? (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      ) : null}
    </fieldset>
  );
}
