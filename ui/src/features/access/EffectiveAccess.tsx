// "Your access" in plain language, shared by the folder Access tab and the home access card. A
// one-line summary first (role badge, one sentence, where it was granted), then one compact
// table (areas x View/Create/Edit/Publish/Delete/Administer: a check, a muted dash, or blank when
// the action does not apply), each row expanding to the readable permission labels, and the raw
// ids behind one "Show technical permission names" disclosure. Every caller (full or partial
// access, home card or folder Access tab) sees only the summary by default: the table, the "No
// access" line and the technical names sit behind one collapsed "See details". Copy only: nothing
// here decides access.
import { Link } from "@tanstack/react-router";
import { Check, ChevronDown } from "lucide-react";
import { useId, useState } from "react";
import { Badge } from "@/components/Badge";
import { SelectField } from "@/components/form";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { cx } from "@/lib/cx";
import { compareText } from "@/lib/text";
import {
  AREA_COPY,
  CAPABILITIES,
  CAPABILITY_LABELS,
  PERMISSION_LABELS,
  areaMissing,
  hasFullAccess,
  partialNote,
  permissionLabel,
  summarizeAccess,
  type AreaSummary,
  type CellState,
} from "./permissionLabels";
import { roleLabel, roleMeaning } from "./roles";
import styles from "./EffectiveAccess.module.css";

export type RoleSource = {
  folderId: string;
  /** Readable folder path ("Acme / Payments"), or "Root". */
  label: string;
  /** True when the grant was made at the folder being described. */
  here: boolean;
  /** The group the role comes through, when not granted directly. */
  viaGroup?: string | null;
};

export type HeldRole = { role: string; sources: RoleSource[] };

export type FolderRef = { id: string; label: string };

export type InheritanceState =
  | { kind: "top" }
  | { kind: "open"; ancestors: FolderRef[] }
  | { kind: "blocked"; at: FolderRef | null; here: boolean };

/** What the caller can do in ONE folder (and below), for the "Show access at" selector. */
export type AccessScope = {
  id: string;
  /** Readable folder path ("Acme / Payments"), or "Root". */
  label: string;
  permissions: readonly string[];
};

export type EffectiveAccessProps = {
  roles: HeldRole[];
  permissions: readonly string[];
  /**
   * Per-folder answers. With 2+ the details gain a "Show access at" selector and the table, the
   * "No access" line and the technical names describe only the selected folder; with one the
   * details are unchanged and show no selector; without them `permissions` is used as given.
   */
  scopes?: AccessScope[];
  /** The folder selected first (defaults to the first scope). */
  initialScopeId?: string;
  /** Where the permissions apply, as the end of a sentence ("here and below"). */
  where: string;
  /** The summary sentence when every area is fully held ("Full access to everything."). */
  fullAccess?: string;
  inheritance?: InheritanceState;
  /** Shown when no role is held. */
  emptyRoles?: string;
};

export function EffectiveAccess({
  roles,
  permissions,
  scopes,
  initialScopeId,
  where,
  fullAccess = `Full access ${where}.`,
  inheritance,
  emptyRoles = "You hold no role here.",
}: EffectiveAccessProps) {
  // One scope is the same answer as the union, so only 2+ scopes switch the view.
  const selectId = useId();
  const scopeList = scopes !== undefined && scopes.length > 1 ? scopes : [];
  const [chosenId, setChosenId] = useState(initialScopeId);
  const scope =
    scopeList.find((candidate) => candidate.id === chosenId) ?? scopeList[0];
  // A scoped view describes the selected folder only; the role lines above stay the full list.
  const shown = scope?.permissions ?? permissions;
  const shownWhere = scope ? `at ${scope.label} and below` : where;
  const summary = summarizeAccess(shown, shownWhere);
  const full =
    permissions.length > 0 &&
    hasFullAccess(summarizeAccess(permissions, where));
  const active = summary.areas.filter((area) => area.held.length > 0);
  const none = summary.areas.filter((area) => area.held.length === 0);

  const details = (
    <>
      {scopeList.length > 1 ? (
        <SelectField
          id={selectId}
          label="Show access at"
          value={scope?.id}
          onChange={(event) => {
            setChosenId(event.target.value);
          }}
          options={scopeList.map((candidate) => ({
            value: candidate.id,
            label: candidate.label,
          }))}
        />
      ) : null}
      {active.length === 0 ? (
        <p className={styles.muted}>You have no permissions {shownWhere}.</p>
      ) : (
        <AccessTable areas={active} where={shownWhere} />
      )}
      {active.length > 0 && none.length > 0 ? (
        <p className={styles.muted}>
          No access: {none.map((area) => area.label).join(", ")}
        </p>
      ) : null}
      {shown.length > 0 ? <TechnicalNames permissions={shown} /> : null}
    </>
  );

  return (
    <div className={styles.root}>
      <div className={styles.summaryBlock}>
        {roles.length === 0 ? (
          <p className={styles.muted}>{emptyRoles}</p>
        ) : (
          <ul className={styles.roles}>
            {roles.map((held) => (
              <li key={held.role} className={styles.role}>
                <Badge intent="brand">{roleLabel(held.role)}</Badge>
                <span className={styles.roleText}>
                  {full && roles.length === 1
                    ? fullAccess
                    : roleMeaning(held.role) || "A platform role."}{" "}
                  <span className={styles.muted}>
                    <RoleSources sources={held.sources} />
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
        {full && roles.length !== 1 ? (
          <p className={styles.strong}>{fullAccess}</p>
        ) : null}
        {inheritance ? <InheritanceLine state={inheritance} /> : null}
      </div>
      <details className={styles.disclosure}>
        <summary className={styles.disclosureSummary}>
          <span>See details</span>
          <ChevronDown
            size={16}
            strokeWidth={2}
            aria-hidden="true"
            className={styles.chevron}
          />
        </summary>
        <div className={styles.disclosurePanel}>{details}</div>
      </details>
    </div>
  );
}

function Mark({ state }: { state: CellState }) {
  switch (state) {
    case "yes":
      return (
        <>
          <Check
            size={16}
            strokeWidth={2.25}
            aria-hidden="true"
            className={styles.check}
          />
          <VisuallyHidden>Allowed</VisuallyHidden>
        </>
      );
    case "partly":
      return (
        <>
          <Check
            size={16}
            strokeWidth={2.25}
            aria-hidden="true"
            className={styles.check}
          />
          <VisuallyHidden>Partly allowed</VisuallyHidden>
        </>
      );
    case "no":
      return (
        <>
          <span aria-hidden="true" className={styles.dash}>
            —
          </span>
          <VisuallyHidden>Not allowed</VisuallyHidden>
        </>
      );
    default:
      return <VisuallyHidden>Not applicable</VisuallyHidden>;
  }
}

function AccessTable({
  areas,
  where,
}: {
  areas: AreaSummary[];
  where: string;
}) {
  const id = useId();
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const toggle = (area: string) => {
    setOpen((previous) => {
      const next = new Set(previous);
      if (next.has(area)) next.delete(area);
      else next.add(area);
      return next;
    });
  };
  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>What you can do {where}</caption>
        <thead>
          <tr>
            <th scope="col">Area</th>
            {CAPABILITIES.map((capability) => (
              <th key={capability} scope="col" className={styles.capCol}>
                {CAPABILITY_LABELS[capability]}
              </th>
            ))}
          </tr>
        </thead>
        {areas.map((area) => {
          const isOpen = open.has(area.area);
          const note = partialNote(area);
          const panelId = `${id}-${area.area}`;
          const missing = areaMissing(area);
          return (
            <tbody key={area.area}>
              <tr>
                <th scope="row" className={styles.rowHeader}>
                  <button
                    type="button"
                    className={styles.expand}
                    aria-expanded={isOpen}
                    aria-controls={panelId}
                    onClick={() => {
                      toggle(area.area);
                    }}
                  >
                    <ChevronDown
                      size={16}
                      strokeWidth={2}
                      aria-hidden="true"
                      className={cx(
                        styles.rowChevron,
                        isOpen && styles.rowChevronOpen,
                      )}
                    />
                    <span>{area.label}</span>
                  </button>
                  {note ? <div className={styles.note}>{note}</div> : null}
                </th>
                {area.cells.map((cell) => (
                  <td key={cell.capability} className={styles.capCell}>
                    <Mark state={cell.state} />
                  </td>
                ))}
              </tr>
              <tr id={panelId} hidden={!isOpen} className={styles.panelRow}>
                <td colSpan={CAPABILITIES.length + 1}>
                  <div className={styles.lists}>
                    <div>
                      <p className={styles.listTitle}>You can</p>
                      <ul className={styles.list}>
                        {area.held.map((permission) => (
                          <li key={permission}>
                            {PERMISSION_LABELS[permission].label}
                          </li>
                        ))}
                      </ul>
                    </div>
                    {missing.length > 0 ? (
                      <div>
                        <p className={styles.listTitle}>Not included</p>
                        <ul className={cx(styles.list, styles.listMuted)}>
                          {missing.map((permission) => (
                            <li key={permission}>
                              {PERMISSION_LABELS[permission].label}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                  </div>
                </td>
              </tr>
            </tbody>
          );
        })}
      </table>
    </div>
  );
}

function TechnicalNames({ permissions }: { permissions: readonly string[] }) {
  return (
    <details className={styles.disclosure}>
      <summary className={cx(styles.disclosureSummary, styles.quiet)}>
        <span>Show technical permission names</span>
        <ChevronDown
          size={16}
          strokeWidth={2}
          aria-hidden="true"
          className={styles.chevron}
        />
      </summary>
      <div className={styles.disclosurePanel}>
        <p className={styles.muted}>
          The permission ids the API checks, as used in API errors and logs.
        </p>
        <dl className={styles.technical}>
          {[...permissions].sort(compareText).map((permission) => {
            const label = permissionLabel(permission);
            return (
              <div key={permission} className={styles.techRow}>
                <dt>
                  <code className={styles.code}>{permission}</code>
                </dt>
                <dd>
                  {label ? (
                    <>
                      {label.label}{" "}
                      <span className={styles.muted}>
                        ({AREA_COPY[label.area].label})
                      </span>
                    </>
                  ) : (
                    <span className={styles.muted}>Not described yet.</span>
                  )}
                </dd>
              </div>
            );
          })}
        </dl>
      </div>
    </details>
  );
}

function FolderLink({ folder }: { folder: FolderRef }) {
  return (
    <Link
      to="/folders/$folderId"
      params={{ folderId: folder.id }}
      className={styles.link}
    >
      {folder.label}
    </Link>
  );
}

function RoleSources({ sources }: { sources: RoleSource[] }) {
  if (sources.length === 0) return <>Granted on a folder above.</>;
  return (
    <>
      {sources.map((source, index) => (
        <span key={`${source.folderId}-${source.viaGroup ?? ""}`}>
          {index === 0 ? "Granted " : "; also "}
          {source.here ? (
            "on this folder"
          ) : (
            <>
              on{" "}
              <FolderLink
                folder={{ id: source.folderId, label: source.label }}
              />
            </>
          )}
          {source.viaGroup ? ` through the group ${source.viaGroup}` : ""}
        </span>
      ))}
      .
    </>
  );
}

function joinRefs(refs: FolderRef[]) {
  return refs.map((ref, index) => (
    <span key={ref.id}>
      {index === 0 ? "" : index === refs.length - 1 ? " and " : ", "}
      <FolderLink folder={ref} />
    </span>
  ));
}

function InheritanceLine({ state }: { state: InheritanceState }) {
  return (
    <p className={styles.muted}>
      {state.kind === "top" ? (
        "This is the top of the hierarchy, so nothing is inherited here."
      ) : state.kind === "open" ? (
        state.ancestors.length === 0 ? (
          "Roles granted on the folders above also apply here."
        ) : (
          <>Roles granted on {joinRefs(state.ancestors)} also apply here.</>
        )
      ) : state.here ? (
        "Inheritance is blocked here: roles granted above this folder do not apply here or below (Global admins are exempt)."
      ) : state.at ? (
        <>
          Inheritance is blocked at <FolderLink folder={state.at} />: roles
          granted above it do not apply here (Global admins are exempt).
        </>
      ) : (
        "Inheritance is blocked above this folder: roles granted higher up do not apply here (Global admins are exempt)."
      )}
    </p>
  );
}
