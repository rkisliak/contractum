// Create or edit a subscriber card (D-UI-13-01/02): name, description, contacts and delivery
// channels. The webhook URL is a PATCH-only field (the create schema has none), so it shows when
// editing. A URL the server refuses (SSRF guard) renders the API detail. When the PATCH creates the
// first webhook secret the API returns it once; the caller shows it in SecretRevealDialog.
import { useNavigate } from "@tanstack/react-router";
import { Plus, Trash2 } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import { mapFieldErrors } from "@/api/fieldErrors";
import type { Problem } from "@/api/problem";
import type { CardContactIn, SubscriberCard } from "@/api/types";
import { Button, IconButton } from "@/components/Button";
import { Dialog } from "@/components/Dialog";
import {
  Checkbox,
  nameRuleError,
  SelectField,
  TextareaField,
  TextField,
  type SelectOption,
} from "@/components/form";
import { ProblemAlert } from "@/components/ProblemAlert";
import { TextLink } from "@/components/TextLink";
import { useCapabilities } from "@/features/access/useCapabilities";
import { hasGlobal, useMe } from "@/features/access/useMe";
import { problemOf } from "@/lib/unexpected";
import { useCardWrites } from "./api";
import styles from "./Cards.module.css";

const CANNOT_CREATE = "You cannot create cards in this folder.";
const HOST_NOT_ALLOWED =
  "This host is not on the list of allowed webhook hosts.";

type ContactRow = {
  key: number;
  kind: CardContactIn["kind"];
  name: string;
  value: string;
};

const contactKinds = [
  { value: "email", label: "Email" },
  { value: "url", label: "Link" },
  { value: "text", label: "Text" },
];

let nextKey = 1;

function rowsOf(card?: SubscriberCard): ContactRow[] {
  return (card?.contacts ?? []).map((contact) => ({
    key: nextKey++,
    kind: contact.kind,
    name: contact.name ?? "",
    value: contact.value,
  }));
}

export function CardFormDialog({
  open,
  onOpenChange,
  folderId,
  folderName,
  folderChoices,
  initialFolderId,
  card,
  onSecret,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Create mode: the folder the card is created in. */
  folderId?: string;
  folderName?: string;
  /** Create mode without a fixed folder: every non-archived folder in scope to choose from. */
  folderChoices?: SelectOption[];
  /** Create mode without a fixed folder: the folder chosen when the dialog opens. */
  initialFolderId?: string;
  /** Edit mode: the card being changed. */
  card?: SubscriberCard;
  /** Edit mode: the first webhook secret, returned once by the PATCH that created it. */
  onSecret?: (secret: string) => void;
}) {
  const navigate = useNavigate();
  const { create, update } = useCardWrites();
  const editing = card !== undefined;
  const [name, setName] = useState(card?.name ?? "");
  const [description, setDescription] = useState(card?.description ?? "");
  const [contacts, setContacts] = useState<ContactRow[]>(() => rowsOf(card));
  const [emailEnabled, setEmailEnabled] = useState(
    card?.email_enabled ?? false,
  );
  const [webhookEnabled, setWebhookEnabled] = useState(
    card?.webhook_enabled ?? false,
  );
  const [webhookUrl, setWebhookUrl] = useState(card?.webhook_url ?? "");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<Problem | null>(null);
  // The webhook host is not on the Global admin's allow-list (it is mandatory): the field says so
  // and, below it, where to add the host (a link for a Global admin, a pointer for anyone else).
  const [hostRefused, setHostRefused] = useState(false);
  const me = useMe();
  const canOpenSettings = hasGlobal(me.data, "settings:read");
  // Only a changed name is checked: a legacy stored name never blocks editing the rest.
  const nameError =
    editing && name.trim() === card.name ? undefined : nameRuleError(name);
  const choosing = !editing && folderId === undefined;
  const [chosenFolder, setChosenFolder] = useState(initialFolderId ?? "");
  const chosenCaps = useCapabilities(chosenFolder || null);
  const targetFolder = folderId ?? chosenFolder;
  const cannotCreate =
    choosing &&
    chosenFolder !== "" &&
    !chosenCaps.isPending &&
    !chosenCaps.can("card:create");
  const formId = editing ? "edit-card-form" : "create-card-form";
  const pending = create.isPending || update.isPending;

  function reset() {
    setName(card?.name ?? "");
    setDescription(card?.description ?? "");
    setContacts(rowsOf(card));
    setEmailEnabled(card?.email_enabled ?? false);
    setWebhookEnabled(card?.webhook_enabled ?? false);
    setWebhookUrl(card?.webhook_url ?? "");
    setChosenFolder(initialFolderId ?? "");
    setErrors({});
    setProblem(null);
    setHostRefused(false);
  }

  function close() {
    reset();
    onOpenChange(false);
  }

  function onError(error: unknown) {
    const found = problemOf(error);
    if (found.kind === "problem" && found.slug === "webhook-host-not-allowed") {
      setErrors({ webhookUrl: HOST_NOT_ALLOWED });
      setHostRefused(true);
      setProblem(null);
      return;
    }
    setHostRefused(false);
    const mapped = mapFieldErrors(found, {
      "/name": "name",
      "/description": "description",
      "/webhook_url": "webhookUrl",
    });
    setErrors(mapped.fieldErrors);
    setProblem(found);
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const found: Record<string, string> = {};
    if (choosing && chosenFolder === "")
      found.folder = "Choose the folder the card belongs to.";
    else if (cannotCreate) found.folder = CANNOT_CREATE;
    if (name.trim() === "") found.name = "Enter a name for the card.";
    else if (nameError !== undefined) found.name = nameError;
    if (description.trim() === "")
      found.description = "Describe who this card represents.";
    if (editing && webhookEnabled && webhookUrl.trim() === "")
      found.webhookUrl = "Enter the webhook URL, or turn webhook delivery off.";
    contacts.forEach((row) => {
      if (row.value.trim() === "")
        found[`contact-${String(row.key)}`] =
          "Enter a value or remove this contact.";
    });
    setErrors(found);
    setProblem(null);
    setHostRefused(false);
    if (Object.keys(found).length > 0) return;
    const payload: CardContactIn[] = contacts.map((row) => ({
      kind: row.kind,
      value: row.value.trim(),
      ...(row.name.trim() === "" ? {} : { name: row.name.trim() }),
    }));
    if (editing) {
      update.mutate(
        {
          cardId: card.id,
          body: {
            ...(name.trim() === card.name ? {} : { name: name.trim() }),
            description: description.trim(),
            contacts: payload,
            email_enabled: emailEnabled,
            webhook_enabled: webhookEnabled,
            ...(webhookUrl.trim() === ""
              ? {}
              : { webhook_url: webhookUrl.trim() }),
          },
        },
        {
          onSuccess: (saved) => {
            onOpenChange(false);
            if (saved.webhook_secret) onSecret?.(saved.webhook_secret);
          },
          onError,
        },
      );
      return;
    }
    create.mutate(
      {
        folderId: targetFolder,
        name: name.trim(),
        description: description.trim(),
        contacts: payload,
        emailEnabled,
      },
      {
        onSuccess: (created) => {
          close();
          void navigate({
            to: "/cards/$cardId",
            params: { cardId: created.id },
          });
        },
        onError,
      },
    );
  }

  function patchRow(key: number, change: Partial<ContactRow>) {
    setContacts((rows) =>
      rows.map((row) => (row.key === key ? { ...row, ...change } : row)),
    );
  }

  const title = editing ? "Edit card" : "Create card";
  return (
    <Dialog
      open={open}
      size={512}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title={title}
      description={
        editing
          ? "Change how this card is described and how it is notified."
          : choosing
            ? "Choose the folder the card belongs to. Subscribe it to contract versions next."
            : `The card is created in ${folderName ?? "this folder"}. Subscribe it to contract versions next.`
      }
      footer={
        <>
          <Button variant="secondary" size="small" onClick={close}>
            Go back
          </Button>
          <Button
            variant="primary"
            size="small"
            type="submit"
            form={formId}
            busy={pending}
            busyLabel={editing ? "Saving…" : "Creating…"}
          >
            {editing ? "Save card" : "Create card"}
          </Button>
        </>
      }
    >
      <form id={formId} className={styles.form} noValidate onSubmit={onSubmit}>
        {problem ? (
          <ProblemAlert
            problem={problem}
            context="action"
            fieldLabels={{
              "/name": "Name",
              "/description": "Description",
              "/webhook_url": "Webhook URL",
            }}
          />
        ) : null}
        {choosing ? (
          <SelectField
            id="card-folder"
            label="Folder"
            placeholder="Choose a folder"
            options={folderChoices ?? []}
            value={chosenFolder}
            error={errors.folder}
            helper={cannotCreate && !errors.folder ? CANNOT_CREATE : undefined}
            onChange={(event) => {
              setChosenFolder(event.target.value);
            }}
          />
        ) : null}
        <TextField
          id="card-name"
          label="Name"
          autoComplete="off"
          maxLength={200}
          value={name}
          error={errors.name ?? nameError}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
        <TextareaField
          id="card-description"
          label="Description"
          rows={3}
          maxLength={2000}
          value={description}
          error={errors.description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Contacts</legend>
          {contacts.length === 0 ? (
            <p className={styles.muted}>
              No contacts. Add who to reach about this card.
            </p>
          ) : null}
          {contacts.map((row, index) => (
            <div key={row.key} className={styles.contactRow}>
              <SelectField
                id={`contact-kind-${String(row.key)}`}
                label={`Contact ${String(index + 1)} kind`}
                controlSize="small"
                options={contactKinds}
                value={row.kind}
                onChange={(event) => {
                  patchRow(row.key, {
                    kind: event.target.value as CardContactIn["kind"],
                  });
                }}
              />
              <TextField
                id={`contact-name-${String(row.key)}`}
                label="Label"
                optional
                autoComplete="off"
                controlSize="small"
                maxLength={100}
                value={row.name}
                onChange={(event) => {
                  patchRow(row.key, { name: event.target.value });
                }}
              />
              <TextField
                id={`contact-value-${String(row.key)}`}
                label="Value"
                autoComplete="off"
                controlSize="small"
                value={row.value}
                error={errors[`contact-${String(row.key)}`]}
                onChange={(event) => {
                  patchRow(row.key, { value: event.target.value });
                }}
              />
              <IconButton
                aria-label={`Remove contact ${String(index + 1)}`}
                icon={Trash2}
                size={32}
                variant="ghost"
                onClick={() => {
                  setContacts((rows) => rows.filter((r) => r.key !== row.key));
                }}
              />
            </div>
          ))}
          {contacts.length < 20 ? (
            <div>
              <Button
                variant="secondary"
                size="small"
                icon={Plus}
                onClick={() => {
                  setContacts((rows) => [
                    ...rows,
                    { key: nextKey++, kind: "email", name: "", value: "" },
                  ]);
                }}
              >
                Add contact
              </Button>
            </div>
          ) : null}
        </fieldset>
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Delivery</legend>
          <Checkbox
            id="card-email-enabled"
            label="Email notices to the email contacts"
            checked={emailEnabled}
            onChange={(event) => {
              setEmailEnabled(event.target.checked);
            }}
          />
          {editing ? (
            <>
              <Checkbox
                id="card-webhook-enabled"
                label="Deliver notices to a webhook"
                checked={webhookEnabled}
                onChange={(event) => {
                  setWebhookEnabled(event.target.checked);
                }}
              />
              <TextField
                id="card-webhook-url"
                label="Webhook URL"
                optional={!webhookEnabled}
                type="url"
                autoComplete="off"
                helper="HTTPS endpoint that receives signed notice deliveries."
                value={webhookUrl}
                error={errors.webhookUrl}
                onChange={(event) => {
                  setWebhookUrl(event.target.value);
                }}
              />
              {hostRefused ? (
                <p className={styles.muted}>
                  {canOpenSettings ? (
                    <TextLink to="/admin/settings?tab=notifications">
                      Add it in Settings
                    </TextLink>
                  ) : (
                    "Ask a Global admin to add it in Administration > Settings > Notifications."
                  )}
                </p>
              ) : null}
            </>
          ) : (
            <p className={styles.muted}>
              A webhook can be configured on the card after it is created.
            </p>
          )}
        </fieldset>
      </form>
    </Dialog>
  );
}
