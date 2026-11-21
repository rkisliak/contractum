// Plain-language labels for every permission id (copy only, never an access decision). Each id
// has a short label, a one-sentence tooltip, the human area it belongs to and the capability
// column it fills in the "Your effective access" matrix (View, Create, Edit, Publish,
// Archive/Delete, Administer). `permissionLabels.test.ts` asserts that every id in
// `permissionRegistry.ts` (the dc_auth snapshot) has an entry here.
import {
  PERMISSION_IDS,
  PERMISSION_TARGETS,
  type PermissionId,
} from "./permissionRegistry";

export const ACCESS_AREAS = [
  "contracts",
  "folders",
  "templates",
  "rulesets",
  "subscriptions",
  "people",
  "audit",
  "platform",
] as const;
export type AccessArea = (typeof ACCESS_AREAS)[number];

export const CAPABILITIES = [
  "view",
  "create",
  "edit",
  "publish",
  "archive",
  "administer",
] as const;
export type Capability = (typeof CAPABILITIES)[number];

export const CAPABILITY_LABELS: Record<Capability, string> = {
  view: "View",
  create: "Create",
  edit: "Edit",
  publish: "Publish",
  archive: "Delete / Archive",
  administer: "Administer",
};

type AreaCopy = {
  label: string;
  /** The object of the area's sentence ("contracts", "rule sets and templates"). */
  noun: string;
  /** The verb each capability column contributes to the sentence. */
  verbs: Partial<Record<Capability, string>>;
};

export const AREA_COPY: Record<AccessArea, AreaCopy> = {
  contracts: {
    label: "Contracts & versions",
    noun: "contracts",
    verbs: {
      view: "view",
      create: "create",
      edit: "edit",
      publish: "publish",
      archive: "delete or restore",
    },
  },
  folders: {
    label: "Folders",
    noun: "folders",
    verbs: {
      view: "browse",
      create: "create",
      edit: "rename",
      archive: "archive or unarchive",
      administer: "control inheritance on",
    },
  },
  templates: {
    label: "Templates",
    noun: "templates",
    verbs: {
      view: "view",
      create: "create",
      edit: "edit",
      publish: "publish",
      archive: "deprecate",
    },
  },
  rulesets: {
    label: "Rule sets & governance",
    noun: "rule sets",
    verbs: {
      view: "view and test",
      create: "create",
      edit: "edit",
      publish: "publish",
      archive: "deprecate",
      administer: "make mandatory",
    },
  },
  subscriptions: {
    label: "Subscriptions",
    noun: "subscriber cards",
    verbs: {
      view: "view",
      create: "create",
      edit: "edit",
      administer: "manage every one of the",
    },
  },
  people: {
    label: "People & access",
    noun: "users, groups, service accounts, tokens and grants",
    verbs: {
      view: "see",
      create: "create",
      edit: "change",
      archive: "deactivate",
      administer: "administer",
    },
  },
  audit: {
    label: "Audit",
    noun: "the audit log",
    verbs: { view: "search" },
  },
  platform: {
    label: "Platform",
    noun: "platform services",
    verbs: {
      view: "query",
      create: "send mail through",
      administer: "administer",
    },
  },
};

export type PermissionLabel = {
  label: string;
  /** One sentence for the tooltip. */
  tooltip: string;
  area: AccessArea;
  capability: Capability;
};

/** A permission's short label and its one-sentence tooltip. */
type Copy = readonly [label: string, tooltip: string];

/** One area's permissions, grouped by the capability column they fill. */
type AreaTable = Partial<
  Record<Capability, Partial<Record<PermissionId, Copy>>>
>;

/**
 * The copy table: area, then capability column, then each id with its label and tooltip. The
 * area and the capability come from the nesting, so no entry repeats them.
 */
const LABEL_TABLE: Record<AccessArea, AreaTable> = {
  contracts: {
    view: {
      "contract:list": [
        "List contracts",
        "See contracts in lists and search results you have access to.",
      ],
      "contract:view": [
        "Open contract pages",
        "Open a contract's page; what it shows depends on your other contract permissions.",
      ],
      "contract:read-metadata": [
        "See contract details",
        "See a contract's name, owner, status and versions, but not its document.",
      ],
      "contract:read-published": [
        "Read published versions",
        "Read and download the documents of published contract versions.",
      ],
      "contract:read": [
        "Read contracts and drafts",
        "Read every contract document here, including drafts and unpublished versions.",
      ],
      "contract:validate": [
        "Validate contracts",
        "Check a contract against the rule sets that apply here, without changing anything.",
      ],
    },
    create: {
      "contract:create": [
        "Create contracts",
        "Create new contracts in this folder and the folders below it.",
      ],
    },
    edit: {
      "contract:update": [
        "Edit contract drafts",
        "Create, edit and discard drafts of a contract.",
      ],
    },
    publish: {
      "contract:publish": [
        "Publish versions",
        "Publish drafts as versions and move versions through their lifecycle (preview, latest, deprecated).",
      ],
    },
    archive: {
      "contract:delete": [
        "Delete contracts",
        "Soft-delete a contract; an administrator can restore it.",
      ],
      "contract:restore": [
        "Restore deleted contracts",
        "Bring a soft-deleted contract back.",
      ],
    },
  },
  folders: {
    view: {
      "folder:list": [
        "List folders",
        "See the folders you have access to in the folder tree.",
      ],
      "folder:read": [
        "Open folders",
        "Open a folder and see its details and contents.",
      ],
    },
    create: {
      "folder:create": [
        "Create subfolders",
        "Create organizations, projects or teams inside a folder.",
      ],
    },
    edit: {
      "folder:update": [
        "Rename folders",
        "Change a folder's display name. Its slug never changes.",
      ],
    },
    archive: {
      "folder:archive": [
        "Archive folders",
        "Archive a folder and everything below it. Nothing is deleted.",
      ],
      "folder:delete": [
        "Delete folders",
        "Delete an empty Organization, Project or Team folder.",
      ],
      "folder:unarchive": [
        "Unarchive folders",
        "Bring an archived folder back.",
      ],
    },
    administer: {
      "folder:set-block": [
        "Block or allow inheritance",
        "Stop (or restart) grants made above a folder from applying to it and below.",
      ],
    },
  },
  templates: {
    view: {
      "template:list": [
        "List templates",
        "See templates in lists you have access to.",
      ],
      "template:read-published": [
        "Read published templates",
        "Use published template versions to start new contracts.",
      ],
      "template:read": [
        "Read template drafts",
        "Read templates including their drafts.",
      ],
    },
    create: {
      "template:create": ["Create templates", "Create new contract templates."],
    },
    edit: {
      "template:update": [
        "Edit template drafts",
        "Change the draft of a template.",
      ],
    },
    publish: {
      "template:publish": [
        "Publish templates",
        "Publish a template draft as a new version.",
      ],
    },
    archive: {
      "template:deprecate": [
        "Deprecate templates",
        "Mark a template version as no longer recommended.",
      ],
    },
  },
  rulesets: {
    view: {
      "ruleset:read-published": [
        "Read published rule sets",
        "Read the published rules that contracts here must pass, including the one blocking you.",
      ],
      "ruleset:read": [
        "Read rule set drafts",
        "Read rule sets including their drafts and rollout.",
      ],
      "ruleset:dry-run": [
        "Run rule dry-runs",
        "Evaluate a rule set version against existing contracts without enforcing it.",
      ],
      "governance:drift-read": [
        "See compliance drift",
        "See which published contracts no longer pass the rules that apply to them.",
      ],
    },
    create: {
      "ruleset:create": ["Create rule sets", "Create new rule sets."],
    },
    edit: {
      "ruleset:update": [
        "Edit rule set drafts",
        "Change the rules in a rule set draft.",
      ],
    },
    publish: {
      "ruleset:publish": [
        "Publish rule sets",
        "Publish a rule set draft as a new version.",
      ],
    },
    archive: {
      "ruleset:deprecate": [
        "Deprecate rule sets",
        "Mark a rule set version as no longer used.",
      ],
    },
    administer: {
      "ruleset:mandate": [
        "Make rule sets mandatory",
        "Switch enforcement: a mandatory rule set blocks publishing contracts that break it.",
      ],
    },
  },
  subscriptions: {
    view: {
      "card:read": [
        "View subscriber cards",
        "Open the subscriber cards you can see and their subscriptions.",
      ],
    },
    create: {
      "card:create": [
        "Create subscriber cards",
        "Create a card that records which contract versions a consumer depends on.",
      ],
    },
    edit: {
      "card:write": [
        "Edit subscriber cards",
        "Change cards you belong to: details, members and subscriptions.",
      ],
    },
    administer: {
      "card:manage": [
        "Manage every card here",
        "Act as an administrator of every subscriber card in the folder and below.",
      ],
      "card:manage-secret": [
        "Rotate webhook secrets",
        "Issue or rotate a card's webhook signing secret (needs an interactive sign-in).",
      ],
    },
  },
  people: {
    view: {
      "me:read": [
        "See your own profile",
        "See who you are signed in as and what you may do.",
      ],
      "principal:read-names": [
        "See who changed things",
        "See the names of the people and accounts that created, edited or published something.",
      ],
      "pat:list": [
        "List your access tokens",
        "See your own personal access tokens (never their secrets).",
      ],
      "user:read": ["View local users", "See local user accounts."],
      "grant:read": ["See who has access", "See the grants made at a folder."],
      "group:list": ["List groups", "See the local groups you have access to."],
      "group:read": [
        "View groups and members",
        "Open a local group and see its members.",
      ],
      "entra:list": [
        "List Entra registrations",
        "See the Entra users, groups and service principals registered.",
      ],
      "entra:read": [
        "View Entra registrations",
        "Open an Entra user, group or service principal registration.",
      ],
      "service-account:list": [
        "List service accounts",
        "See the service accounts you have access to.",
      ],
      "service-account:read": [
        "View service accounts",
        "Open a service account and see its keys (never their secrets).",
      ],
    },
    create: {
      "pat:manage": [
        "Create and revoke your tokens",
        "Create and revoke your own personal access tokens (needs an interactive sign-in).",
      ],
      "user:create": ["Create local users", "Create local user accounts."],
      "group:create": [
        "Create groups",
        "Create local groups owned by a folder.",
      ],
      "service-account:create": [
        "Create service accounts",
        "Create service accounts for pipelines and other services.",
      ],
    },
    edit: {
      "me:update": [
        "Change your display name",
        "Change the display name of your own local account (needs an interactive sign-in).",
      ],
      "password:rotate": [
        "Set a new password",
        "Replace a password that must be changed before you continue.",
      ],
      "user:update": [
        "Edit users",
        "Change a local user's display name (needs an interactive sign-in).",
      ],
      "group:manage": [
        "Change groups and members",
        "Rename a local group and add or remove its members.",
      ],
      "service-account:manage": [
        "Rename service accounts",
        "Change a service account's name and description.",
      ],
    },
    archive: {
      "user:deactivate": [
        "Deactivate users",
        "Deactivate or reactivate a local user; deactivation revokes their sessions and tokens.",
      ],
      "service-account:deactivate": [
        "Deactivate service accounts",
        "Deactivate or reactivate a service account; deactivation revokes its keys.",
      ],
    },
    administer: {
      "pat:read-all": [
        "See every access token",
        "See the metadata of every personal access token on the platform.",
      ],
      "pat:revoke-any": [
        "Revoke anyone's token",
        "Revoke any personal access token on the platform.",
      ],
      "grant:manage": [
        "Grant and revoke access",
        "Give people, groups and service accounts a role at a folder, or take it away.",
      ],
      "entra:manage": [
        "Register Entra identities",
        "Register, change and remove Entra users, groups and service principals.",
      ],
      "service-account:manage-keys": [
        "Create and revoke keys",
        "Mint and revoke a service account's API keys.",
      ],
    },
  },
  audit: {
    view: {
      "audit:list": [
        "Open the audit log",
        "Open the audit explorer; rows are limited to what you may see.",
      ],
      "audit:query": [
        "Search audit events here",
        "See the recorded changes and access for this folder and below.",
      ],
      "audit:read": [
        "Read raw audit events",
        "Read audit events through the internal service interface.",
      ],
    },
  },
  platform: {
    view: {
      "authz:check": [
        "Ask for access decisions",
        "Services ask the identity service whether a caller may act.",
      ],
      "settings:read": [
        "View platform settings",
        "See the Admin > Settings page: Entra defaults, the banner and the service overview.",
      ],
      "settings:read-public": [
        "See the announcement banner",
        "See the platform announcement and the default values that pre-fill forms.",
      ],
      "settings:read-runtime": [
        "Read runtime settings",
        "Let a platform service read the non-secret runtime settings.",
      ],
    },
    create: {
      "mail:send": [
        "Send notification mail",
        "Send mail through the platform's mail service.",
      ],
      "settings:seed-webhook-hosts": [
        "Seed allowed webhook hosts",
        "Internal: the notification worker fills the allowed webhook hosts once from the webhooks that already exist.",
      ],
    },
    administer: {
      "settings:write": [
        "Change platform settings",
        "Change, export and import platform settings (needs an interactive sign-in).",
      ],
    },
  },
};

export const PERMISSION_LABELS = Object.fromEntries(
  ACCESS_AREAS.flatMap((area) =>
    CAPABILITIES.flatMap((capability) =>
      Object.entries(LABEL_TABLE[area][capability] ?? {}).map(
        ([id, [label, tooltip]]) => [id, { label, tooltip, area, capability }],
      ),
    ),
  ),
) as Record<PermissionId, PermissionLabel>;

const isPermissionId = (id: string): id is PermissionId =>
  (PERMISSION_IDS as readonly string[]).includes(id);

/** The readable label of a permission id; an unknown id reads as itself. */
export function permissionLabel(id: string): PermissionLabel | undefined {
  return isPermissionId(id) ? PERMISSION_LABELS[id] : undefined;
}

/**
 * Permissions every signed-in principal holds implicitly (principal-level gates and the special
 * branches). They are labelled but never counted in the matrix, because the folder answers do
 * not carry them and a "missing" one would read as a gap.
 */
export function isImplicit(id: PermissionId): boolean {
  const target = PERMISSION_TARGETS[id];
  return target === "principal" || target === "special";
}

export type CellState = "yes" | "partly" | "no" | "n/a";

export type AreaCell = {
  capability: Capability;
  state: CellState;
  held: PermissionId[];
  missing: PermissionId[];
};

export type AreaSummary = {
  area: AccessArea;
  label: string;
  cells: AreaCell[];
  /** Explicit permissions held in the area (ids the API returned). */
  held: PermissionId[];
  /** One plain sentence: what the caller can do in the area. */
  sentence: string;
};

export type AccessSummary = {
  areas: AreaSummary[];
  /** Ids the API returned that the registry does not know (a newer backend). */
  unknown: string[];
};

function joinWords(words: string[]): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1] ?? ""}`;
}

/**
 * Groups the permissions into areas and fills each capability cell: "yes" when every permission
 * of that column is held, "partly" when some are, "no" when none are, "n/a" when the area has no
 * permission of that kind. Implicit (principal-level) ids never decide a cell.
 */
export function summarizeAccess(
  permissions: readonly string[],
  where: string,
): AccessSummary {
  const held = new Set(permissions);
  const unknown = permissions.filter((id) => !isPermissionId(id));
  const areas = ACCESS_AREAS.map((area): AreaSummary => {
    const inArea = PERMISSION_IDS.filter(
      (id) => PERMISSION_LABELS[id].area === area,
    );
    const cells = CAPABILITIES.map((capability): AreaCell => {
      const ids = inArea.filter(
        (id) => PERMISSION_LABELS[id].capability === capability,
      );
      const counted = ids.filter((id) => !isImplicit(id));
      const has = ids.filter((id) => held.has(id));
      const countedHas = counted.filter((id) => held.has(id));
      let state: CellState;
      if (counted.length === 0) state = has.length > 0 ? "yes" : "n/a";
      else if (countedHas.length === counted.length) state = "yes";
      else if (countedHas.length > 0) state = "partly";
      else state = "no";
      return {
        capability,
        state,
        held: has,
        missing: counted.filter((id) => !held.has(id)),
      };
    });
    const areaHeld = inArea.filter((id) => held.has(id));
    return {
      area,
      label: AREA_COPY[area].label,
      cells,
      held: areaHeld,
      sentence: areaSentence(area, cells, where),
    };
  });
  return { areas, unknown };
}

function areaSentence(
  area: AccessArea,
  cells: AreaCell[],
  where: string,
): string {
  const copy = AREA_COPY[area];
  const verbs = cells
    .filter((cell) => cell.state === "yes" || cell.state === "partly")
    .map((cell) => copy.verbs[cell.capability])
    .filter((verb): verb is string => verb !== undefined);
  if (verbs.length === 0) return `You cannot work with ${copy.noun} ${where}.`;
  const partial = cells
    .filter((cell) => cell.state === "partly")
    .flatMap((cell) => cell.missing)
    .map((id) => PERMISSION_LABELS[id].label.toLowerCase());
  const base = `You can ${joinWords(verbs)} ${copy.noun} ${where}.`;
  return partial.length === 0
    ? base
    : `${base} Not included: ${joinWords(partial)}.`;
}

const lowerFirst = (text: string) =>
  text.charAt(0).toLowerCase() + text.slice(1);

/** True when every capability of every area is fully held (n/a cells aside). */
export function hasFullAccess(summary: AccessSummary): boolean {
  return summary.areas.every(
    (area) =>
      area.held.length > 0 &&
      area.cells.every((cell) => cell.state === "yes" || cell.state === "n/a"),
  );
}

/** The permissions a partly held area is missing (counted ids only). */
export function areaMissing(area: AreaSummary): PermissionId[] {
  return area.cells
    .filter((cell) => cell.state === "partly" || cell.state === "no")
    .flatMap((cell) => cell.missing);
}

/**
 * One short line under an area with a partly held column ("Can't restore deleted contracts."),
 * naming at most `max` of that column's missing permissions (a dash already says "none"); ""
 * when no column is partly held.
 */
export function partialNote(area: AreaSummary, max = 2): string {
  const missing = area.cells
    .filter((cell) => cell.state === "partly")
    .flatMap((cell) => cell.missing)
    .map((id) => lowerFirst(PERMISSION_LABELS[id].label));
  if (missing.length === 0) return "";
  const shown = missing.slice(0, max);
  const rest = missing.length - shown.length;
  return rest > 0
    ? `Can't ${shown.join(", ")} and ${String(rest)} more.`
    : `Can't ${joinWords(shown)}.`;
}
