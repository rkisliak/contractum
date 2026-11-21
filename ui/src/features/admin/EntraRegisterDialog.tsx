// Register an Entra user, group or service principal (D-UI-12-09). By default the admin searches the
// Entra directory by name (EntraDirectorySearch, quick 261119-ofa) and picks a match; "Enter an object
// ID instead" keeps the paste-a-GUID path, which also works when the directory cannot be searched.
// The server still confirms the object in the Entra directory before saving it; a 503 means the
// directory was unreachable and nothing was changed. One dialog serves the three kinds; the admin
// pages open it from their "New" menu (quick 261119-l30).
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import { SelectField, TextField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import type { EntraDirectoryMatch } from "@/api/types";
import { usePublicSettings } from "@/features/settings/api";
import { useMe } from "@/features/access/useMe";
import { problemOf } from "@/lib/unexpected";
import { EntraDirectorySearch } from "./EntraDirectorySearch";
import {
  idKeys,
  ownerOptions,
  useApi,
  useFolderLookup,
  useSettle,
} from "./shared";
import styles from "./admin.module.css";

export const ENTRA_UNREACHABLE =
  "The Entra directory is unreachable; nothing was changed. Retry.";

const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** A problem as the Entra screens show it: a 503 gets the fixed directory copy. */
export function EntraProblem({ problem }: { problem: Problem }) {
  if (problem.kind === "problem" && problem.status === 503)
    return <Alert intent="warning">{ENTRA_UNREACHABLE}</Alert>;
  return <ProblemAlert problem={problem} context="action" />;
}

export type RegisterKind = "user" | "group" | "service-principal";

const KIND_COPY: Record<RegisterKind, { noun: string; title: string }> = {
  user: { noun: "user", title: "Register Entra user" },
  group: { noun: "group", title: "Register Entra group" },
  "service-principal": {
    noun: "service principal",
    title: "Register Entra service principal",
  },
};

export function EntraRegisterDialog({
  kind,
  open,
  onOpenChange,
}: {
  kind: RegisterKind;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const api = useApi();
  const me = useMe();
  const settle = useSettle();
  const lookup = useFolderLookup();
  const owners = ownerOptions(me.data, lookup.folders, "entra:manage");
  const [objectId, setObjectId] = useState("");
  // Quick 261120-0yc: the Settings defaults pre-fill the tenant (until edited) and the owner (only
  // when it is one of this caller's own owner options). The API never relies on them.
  const prefill = usePublicSettings().data;
  const [tenantEdited, setTenantEdited] = useState<string | null>(null);
  const tenantId = tenantEdited ?? prefill?.entra_default_tenant_id ?? "";
  const setTenantId = setTenantEdited;
  const [owner, setOwner] = useState("");
  const [mode, setMode] = useState<"search" | "object-id">("search");
  // A reset remounts the search, so its typed text goes with the form.
  const [round, setRound] = useState(0);
  const [picked, setPicked] = useState<EntraDirectoryMatch | null>(null);
  const [errors, setErrors] = useState<{
    objectId?: string;
    tenantId?: string;
    owner?: string;
    picked?: string;
  }>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState(false);
  const copy = KIND_COPY[kind];
  const prefillOwner = owners.find(
    (option) => option.value === prefill?.entra_default_owner_folder_id,
  )?.value;
  const ownerValue = owner || prefillOwner || owners[0]?.value || "";

  function reset() {
    setObjectId("");
    setTenantEdited(null);
    setOwner("");
    setMode("search");
    setPicked(null);
    setRound((n) => n + 1);
    setErrors({});
    setProblem(null);
  }

  async function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const searching = mode === "search";
    const oid = (searching ? (picked?.object_id ?? "") : objectId)
      .trim()
      .toLowerCase();
    const tid = (searching && picked ? picked.tenant_id : tenantId)
      .trim()
      .toLowerCase();
    const found: typeof errors = {};
    if (searching) {
      if (picked === null)
        found.picked = `Choose a ${copy.noun} from the results, or enter an object ID instead.`;
    } else if (!GUID.test(oid))
      found.objectId = "Enter the object ID as a GUID.";
    if (tid !== "" && !GUID.test(tid))
      found.tenantId = "Enter the tenant ID as a GUID, or leave it empty.";
    if (kind !== "user" && ownerValue === "") found.owner = "Choose an owner.";
    setErrors(found);
    setProblem(null);
    if (Object.keys(found).length > 0) return;
    const tenant = tid === "" ? null : tid;
    setBusy(true);
    try {
      if (kind === "user") {
        await api.POST("/api/v1/identity/entra/users", {
          body: { object_id: oid, tenant_id: tenant },
        });
        await settle(idKeys.entraUsers);
      } else if (kind === "group") {
        await api.POST("/api/v1/identity/entra/groups", {
          body: {
            object_id: oid,
            tenant_id: tenant,
            owner_folder_id: ownerValue,
          },
        });
        await settle(idKeys.entraGroups);
      } else {
        await api.POST("/api/v1/identity/entra/service-principals", {
          body: {
            object_id: oid,
            tenant_id: tenant,
            owner_folder_id: ownerValue,
          },
        });
        await settle(idKeys.entraSps);
      }
      await settle(idKeys.entraDirectory);
      reset();
      onOpenChange(false);
    } catch (error) {
      const p = problemOf(error);
      if (p.kind === "problem" && p.status === 409)
        setErrors({ objectId: `This ${copy.noun} is already registered.` });
      else setProblem(p);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title={copy.title}
      description={`Search the Entra directory or enter an object ID. The server confirms the ${copy.noun} in the directory before saving it.`}
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
            form={`register-${kind}-form`}
            busy={busy}
            busyLabel="Registering…"
          >
            {copy.title}
          </Button>
        </>
      }
    >
      <form
        id={`register-${kind}-form`}
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          void onSubmit(event);
        }}
      >
        {problem ? <EntraProblem problem={problem} /> : null}
        {kind !== "user" ? (
          <SelectField
            id={`register-${kind}-owner`}
            label="Owner"
            value={ownerValue}
            options={owners}
            error={errors.owner}
            onChange={(event) => {
              setOwner(event.target.value);
              setPicked(null);
            }}
          />
        ) : null}
        {mode === "search" ? (
          <EntraDirectorySearch
            key={round}
            kind={kind}
            id={`register-${kind}`}
            ownerId={kind === "user" ? "" : ownerValue}
            tenantId={tenantId.trim().toLowerCase()}
            picked={picked}
            error={errors.picked}
            onPick={setPicked}
          />
        ) : (
          <TextField
            id={`register-${kind}-oid`}
            label="Object ID"
            autoComplete="off"
            spellCheck={false}
            value={objectId}
            error={errors.objectId}
            helper="The directory object ID (oid), for example 00000000-0000-0000-0000-000000000000."
            onChange={(event) => {
              setObjectId(event.target.value);
            }}
          />
        )}
        <div>
          <Button
            variant="ghost"
            size="small"
            onClick={() => {
              setMode(mode === "search" ? "object-id" : "search");
              setErrors({});
              setProblem(null);
            }}
          >
            {mode === "search"
              ? "Enter an object ID instead"
              : "Search the directory instead"}
          </Button>
        </div>
        <TextField
          id={`register-${kind}-tid`}
          label="Tenant ID (optional)"
          autoComplete="off"
          spellCheck={false}
          value={tenantId}
          error={errors.tenantId}
          helper="Leave empty to use the platform's configured tenant."
          onChange={(event) => {
            setTenantId(event.target.value);
            setPicked(null);
          }}
        />
      </form>
    </Dialog>
  );
}
