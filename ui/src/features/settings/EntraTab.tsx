// Settings > Entra (quick 261120-0yc: E1 to E5). The default tenant and the default owner are
// stored settings (saved with the section ETag); the overview, the secret metadata and Test
// connection are view only: issuer trust stays in Helm. Nothing here ever shows a secret.
import { useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import type { Problem } from "@/api/problem";
import type { Folder, SettingsOut, SettingsSection } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import { Button } from "@/components/Button";
import { Card } from "@/components/Card";
import { SelectField } from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { FolderSelect } from "@/features/folders/FolderSelect";
import { formatDateTime } from "@/features/admin/shared";
import { problemOf } from "@/lib/unexpected";
import { useEntraOverview, useEntraTest, useSaveSection } from "./api";
import { SaveProblem } from "./SaveProblem";
import styles from "./settings.module.css";

const OWNER_KINDS = new Set(["root", "organization"]);
const isOwnerFolder = (folder: Folder) =>
  OWNER_KINDS.has(folder.kind) && !folder.archived;

const TEST_COPY: Record<string, { intent: "info" | "warning"; text: string }> =
  {
    ok: { intent: "info", text: "Connected: Microsoft Graph answered." },
    "not-configured": {
      intent: "warning",
      text: "Microsoft Graph is not configured: the client ID or its secret is missing.",
    },
    "token-refused": {
      intent: "warning",
      text: "Microsoft refused the Graph client credentials. Check the client ID and the client secret.",
    },
    "graph-forbidden": {
      intent: "warning",
      text: "Microsoft Graph refused the request. Check that admin consent was granted for the Graph permissions.",
    },
    "graph-throttled": {
      intent: "warning",
      text: "Microsoft Graph is throttling requests. Try again shortly.",
    },
    "graph-unreachable": {
      intent: "warning",
      text: "Microsoft Graph could not be reached from the platform.",
    },
    "graph-error": {
      intent: "warning",
      text: "Microsoft Graph answered with an error.",
    },
  };

const without = (values: Record<string, unknown>, key: string) =>
  Object.fromEntries(Object.entries(values).filter(([name]) => name !== key));

function DefaultNote({
  label,
  value,
  stored,
  onReset,
  busy,
}: {
  label: string;
  value: string | null | undefined;
  stored: boolean;
  onReset: () => void;
  busy: boolean;
}) {
  return (
    <div className={styles.fieldMeta}>
      <span>
        Default: <span className={styles.ident}>{value ? value : "none"}</span>
      </span>
      <Button
        variant="ghost"
        size="small"
        disabled={!stored}
        aria-label={`Reset to default for ${label}`}
        busy={busy}
        busyLabel="Resetting…"
        onClick={onReset}
      >
        Reset to default
      </Button>
    </div>
  );
}

function EntraForm({
  section,
  choices,
  onReload,
}: {
  section: SettingsSection;
  choices: SettingsOut["choices"];
  onReload: () => void;
}) {
  const save = useSaveSection();
  const stored = section.values;
  const [tenant, setTenant] = useState(
    typeof stored.default_tenant_id === "string"
      ? stored.default_tenant_id
      : "",
  );
  const [owner, setOwner] = useState(
    typeof stored.default_owner_folder_id === "string"
      ? stored.default_owner_folder_id
      : "",
  );
  const [errors, setErrors] = useState<{ tenant?: string; owner?: string }>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  const [saved, setSaved] = useState(false);
  // A new etag (after a save or a reload) means new stored values: take them, keep the "saved"
  // note. State adjusted during render, so the form is not remounted and the note survives.
  const [seen, setSeen] = useState(section.etag);
  if (seen !== section.etag) {
    setSeen(section.etag);
    setTenant(
      typeof section.values.default_tenant_id === "string"
        ? section.values.default_tenant_id
        : "",
    );
    setOwner(
      typeof section.values.default_owner_folder_id === "string"
        ? section.values.default_owner_folder_id
        : "",
    );
    setProblem(null);
  }
  const staleTenant =
    tenant !== "" && !choices.entra_tenants.includes(tenant) ? tenant : null;
  const defaults = section.defaults;
  const defaultTenant =
    typeof defaults.default_tenant_id === "string"
      ? defaults.default_tenant_id
      : null;

  function submit(values: Record<string, unknown>) {
    setProblem(null);
    setErrors({});
    setSaved(false);
    save.mutate(
      { section: section.section, values, etag: section.etag },
      {
        onSuccess: () => {
          setSaved(true);
        },
        onError: (failure) => {
          const found = problemOf(failure);
          const mapped = mapFieldErrors(found, {
            "/default_tenant_id": "tenant",
            "/default_owner_folder_id": "owner",
          });
          setErrors(mapped.fieldErrors);
          setProblem(
            mapped.unmapped.length > 0 ||
              Object.keys(mapped.fieldErrors).length === 0
              ? found
              : null,
          );
        },
      },
    );
  }

  const current = (): Record<string, unknown> => ({
    ...(tenant !== "" ? { default_tenant_id: tenant } : {}),
    ...(owner !== "" ? { default_owner_folder_id: owner } : {}),
  });

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    submit(current());
  }

  return (
    <form className={styles.form} noValidate onSubmit={onSubmit}>
      {problem ? (
        <SaveProblem
          problem={problem}
          onReload={onReload}
          fieldLabels={{
            "/default_tenant_id": "Default tenant",
            "/default_owner_folder_id":
              "Default owner for registered Entra identities",
          }}
        />
      ) : null}
      {saved ? (
        <Alert intent="success" title="Saved">
          Entra defaults saved.
        </Alert>
      ) : null}
      <div className={styles.fieldRow}>
        <SelectField
          id="settings-entra-tenant"
          label="Default tenant"
          helper="Used when a register call or the directory search names no tenant, and pre-filled in the Register dialogs."
          placeholder="Use the platform default"
          value={tenant}
          error={errors.tenant}
          options={[
            ...choices.entra_tenants.map((id) => ({ value: id, label: id })),
            ...(staleTenant
              ? [
                  {
                    value: staleTenant,
                    label: `${staleTenant} (no longer configured)`,
                    disabled: true,
                  },
                ]
              : []),
          ]}
          onChange={(event) => {
            setTenant(event.target.value);
            setSaved(false);
          }}
        />
        {staleTenant ? (
          <Alert intent="warning" title="Saved tenant is not configured">
            The saved default tenant is no longer one of the configured Entra
            tenants, so it is ignored. Choose another or reset it.
          </Alert>
        ) : null}
        <DefaultNote
          label="default tenant"
          value={defaultTenant}
          stored={typeof stored.default_tenant_id === "string"}
          busy={save.isPending}
          onReset={() => {
            setTenant("");
            submit(without(stored, "default_tenant_id"));
          }}
        />
      </div>
      <div className={styles.fieldRow}>
        <FolderSelect
          id="settings-entra-owner"
          label="Default owner for registered Entra identities"
          helper="Pre-fills the Owner field when you register an Entra user, group or service principal. The owner (ROOT or one Organization) decides who manages that identity and where it can be given access. Only used for someone allowed to register in that folder; it grants nothing by itself."
          placeholder="No default owner"
          controlSize="default"
          value={owner}
          filter={isOwnerFolder}
          onChange={(value) => {
            setOwner(value);
            setSaved(false);
          }}
        />
        {errors.owner ? <Alert intent="danger">{errors.owner}</Alert> : null}
        <DefaultNote
          label="default owner"
          value={null}
          stored={typeof stored.default_owner_folder_id === "string"}
          busy={save.isPending}
          onReset={() => {
            setOwner("");
            submit(without(stored, "default_owner_folder_id"));
          }}
        />
      </div>
      <div className={styles.actions}>
        <Button
          type="submit"
          variant="primary"
          size="small"
          busy={save.isPending}
          busyLabel="Saving…"
        >
          Save Entra defaults
        </Button>
      </div>
    </form>
  );
}

function expiryText(expiry: {
  status: string;
  expires_at: string | null;
  days_left: number | null;
  reason: string | null;
}): string {
  if (expiry.status === "known" && expiry.expires_at !== null) {
    const days = expiry.days_left ?? 0;
    return `${formatDateTime(expiry.expires_at)} (${String(days)} days left)`;
  }
  if (expiry.status === "not-configured") return "No Graph client configured";
  return "Unknown";
}

function Overview() {
  const overview = useEntraOverview();
  const test = useEntraTest();
  if (overview.isError) {
    return (
      <ProblemAlert
        problem={problemOf(overview.error)}
        onRetry={() => void overview.refetch()}
      />
    );
  }
  if (overview.data === undefined)
    return <Skeleton label="Entra overview" rows={4} />;
  const data = overview.data;
  const expiry = data.secret_expiry;
  const remind =
    expiry.status === "unknown" ||
    (expiry.status === "known" && (expiry.days_left ?? 0) < 30);
  const result =
    test.data !== undefined ? TEST_COPY[test.data.result] : undefined;
  const testProblem = test.isError ? problemOf(test.error) : null;
  return (
    <Card title="Managed in Helm (view only)">
      <div className={styles.stack}>
        <dl className={styles.facts}>
          <dt>Sign-in</dt>
          <dd>
            {data.signin_configured ? (
              <Badge intent="success">Configured</Badge>
            ) : (
              <Badge intent="neutral">Not configured</Badge>
            )}
          </dd>
          <dt>Tenants</dt>
          <dd>
            {data.tenants.length === 0
              ? "None"
              : data.tenants.map((tenant) => (
                  <div key={tenant.tenant_id}>
                    <span className={styles.ident}>{tenant.tenant_id}</span>
                    {" · "}
                    <span className={styles.ident}>{tenant.issuer}</span>
                  </div>
                ))}
          </dd>
          <dt>Graph client ID</dt>
          <dd className={styles.ident}>{data.graph_client_id ?? "Not set"}</dd>
          <dt>Graph client secret</dt>
          <dd>
            {data.graph_secret_configured ? (
              <Badge intent="success">Secret file present</Badge>
            ) : (
              <Badge intent="neutral">No secret file</Badge>
            )}
          </dd>
          <dt>Secret expires</dt>
          <dd>{expiryText(expiry)}</dd>
        </dl>
        {remind ? (
          <Alert intent="warning" title="Rotate the Graph client secret">
            Rotate the Graph client secret before it expires; see Entra setup.
            {expiry.reason ? ` ${expiry.reason}` : ""}
          </Alert>
        ) : null}
        <div className={styles.actions}>
          <Button
            variant="outline-neutral"
            size="small"
            busy={test.isPending}
            busyLabel="Testing…"
            onClick={() => {
              test.mutate();
            }}
          >
            Test connection
          </Button>
        </div>
        {result ? (
          <Alert intent={result.intent} title="Connection test">
            {result.text}
          </Alert>
        ) : null}
        {testProblem?.kind === "problem" && testProblem.status === 429 ? (
          <Alert intent="warning" title="Too many tests">
            At most 6 connection tests per minute. Try again shortly.
          </Alert>
        ) : testProblem ? (
          <Alert intent="warning" title="The test could not run">
            Try again shortly.
          </Alert>
        ) : null}
      </div>
    </Card>
  );
}

export function EntraTab({
  section,
  choices,
  onReload,
}: {
  section: SettingsSection;
  choices: SettingsOut["choices"];
  onReload: () => void;
}) {
  return (
    <div className={styles.stack}>
      <Card title="Defaults">
        <EntraForm section={section} choices={choices} onReload={onReload} />
      </Card>
      <Overview />
    </div>
  );
}
