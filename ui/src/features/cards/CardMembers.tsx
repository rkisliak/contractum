// The card's Members tab (D-UI-13-02): groups and service accounts linked to the card, picked with
// the shared PrincipalPicker (names, never typed ids) and listed by name. Identity decides whether a
// principal may be linked; a refusal renders the API detail. Removal confirms.
import { UserPlus } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import type { Problem } from "@/api/problem";
import type { CardMember, SubscriberCard } from "@/api/types";
import { Button } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { Dialog } from "@/components/Dialog";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState } from "@/components/states";
import {
  PrincipalPicker,
  type PickedPrincipal,
} from "@/features/admin/PrincipalPicker";
import { usePrincipalNames } from "@/features/admin/usePrincipalNames";
import { formatDateTime } from "@/features/folders/FolderOverview";
import { problemOf } from "@/lib/unexpected";
import { memberTypeLabels, useCardWrites } from "./api";
import styles from "./Cards.module.css";
import { PrincipalName } from "@/components/PrincipalName";

type MemberType = CardMember["member_type"];

const MEMBER_TYPES: MemberType[] = [
  "local_group",
  "entra_group",
  "service_account",
];

function isMemberType(type: string): type is MemberType {
  return (MEMBER_TYPES as string[]).includes(type);
}

function AddMemberDialog({
  open,
  onOpenChange,
  cardId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  cardId: string;
}) {
  const { addMember } = useCardWrites();
  const [picked, setPicked] = useState<PickedPrincipal | null>(null);
  const [error, setError] = useState<string | undefined>();
  const [problem, setProblem] = useState<Problem | null>(null);
  // Remounts the picker on reset, so its type and search start over too.
  const [pickerKey, setPickerKey] = useState(0);

  function reset() {
    setPicked(null);
    setError(undefined);
    setProblem(null);
    setPickerKey((key) => key + 1);
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    if (picked === null || !isMemberType(picked.type)) {
      setError("Choose a group or service account.");
      return;
    }
    setError(undefined);
    addMember.mutate(
      { cardId, memberType: picked.type, memberId: picked.id },
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
      title="Add member"
      description="Members can read this card and manage its subscriptions. Only the creator and folder admins can edit it, archive it or change its members and webhook."
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
            form="add-member-form"
            busy={addMember.isPending}
            busyLabel="Adding…"
          >
            Add member
          </Button>
        </>
      }
    >
      <form
        id="add-member-form"
        className={styles.form}
        noValidate
        onSubmit={onSubmit}
      >
        {problem ? <ProblemAlert problem={problem} context="action" /> : null}
        <PrincipalPicker
          key={pickerKey}
          types={MEMBER_TYPES}
          value={picked}
          error={error}
          onChange={(next) => {
            setPicked(next);
            if (next) setError(undefined);
          }}
        />
      </form>
    </Dialog>
  );
}

export function CardMembers({
  card,
  canManage,
}: {
  card: SubscriberCard;
  canManage: boolean;
}) {
  const { removeMember } = useCardWrites();
  const nameOf = usePrincipalNames();
  const memberName = (row: CardMember) =>
    nameOf(row.member_id) ?? row.member_id;
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<CardMember | null>(null);
  const writable = canManage && card.archived_at === null;

  const columns: Column<CardMember>[] = [
    {
      id: "id",
      header: "Member",
      rowHeader: true,
      sortValue: memberName,
      cell: (row) => {
        const name = nameOf(row.member_id);
        return name ? (
          <span title={row.member_id}>{name}</span>
        ) : (
          <span className={styles.ident}>{row.member_id}</span>
        );
      },
    },
    {
      id: "type",
      header: "Type",
      cell: (row) => memberTypeLabels[row.member_type] ?? row.member_type,
    },
    {
      id: "by",
      header: "Added by",
      cell: (row) => <PrincipalName id={row.added_by} />,
    },
    {
      id: "at",
      header: "Added at",
      sortValue: (row) => row.added_at,
      cell: (row) => formatDateTime(row.added_at),
    },
    {
      id: "actions",
      header: "Actions",
      align: "end",
      cell: (row) =>
        writable ? (
          <Button
            variant="outline-danger"
            size="small"
            aria-label={`Remove member ${memberName(row)}`}
            onClick={() => {
              setRemoving(row);
            }}
          >
            Remove
          </Button>
        ) : null,
    },
  ];

  const addButton = writable ? (
    <Button
      variant="primary"
      size="small"
      icon={UserPlus}
      onClick={() => {
        setAdding(true);
      }}
    >
      Add member
    </Button>
  ) : null;

  return (
    <section className={styles.stack} aria-label="Members">
      {card.members.length > 0 ? (
        <>
          <div className={styles.toolbar}>
            <span />
            {addButton}
          </div>
          <DataTable
            caption={`Members of ${card.name}`}
            captionHidden
            columns={columns}
            rows={card.members}
            getRowId={(row) => `${row.member_type}:${row.member_id}`}
          />
        </>
      ) : (
        <EmptyState
          level={3}
          heading="No members yet"
          body="Link a group or service account so its members can read this card and manage its subscriptions."
          action={addButton}
        />
      )}
      <AddMemberDialog
        open={adding}
        onOpenChange={setAdding}
        cardId={card.id}
      />
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open) setRemoving(null);
        }}
        title="Remove member?"
        body={`${removing ? memberName(removing) : ""} loses its member access to ${card.name}.`}
        objectName="member"
        confirmLabel="Remove member"
        busyLabel="Removing…"
        onConfirm={async () => {
          if (removing === null) return;
          await removeMember.mutateAsync({
            cardId: card.id,
            memberType: removing.member_type,
            memberId: removing.member_id,
          });
        }}
      />
    </section>
  );
}
