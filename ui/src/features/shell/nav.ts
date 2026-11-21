// The navigation registry (UI-SPEC section 6, D-UI-08-22). Every sidebar entry lives here: a
// section, a route, a lucide icon, a label and an optional `visible` predicate over `/me`. Later
// builders add or edit entries here; they never hand-write sidebar markup. A predicate only hides
// an entry (the API still checks every request), so keep predicates lenient.
import type { LucideIcon } from "lucide-react";
import {
  Activity,
  FileText,
  FlaskConical,
  FolderTree,
  Home,
  KeyRound,
  LayoutTemplate,
  ListChecks,
  Bell,
  ScrollText,
  Settings,
  UserCog,
  Users,
  UsersRound,
} from "lucide-react";
import {
  grantedGlobalPermissions,
  holdsAny,
  type HeldAccess,
} from "@/features/access/useHeldAccess";

export type NavEntry = {
  id: string;
  label: string;
  /** A router path. The entry is current when the location starts with it ("/" only exactly). */
  to: string;
  icon: LucideIcon;
  /**
   * Hides the entry when false. Omitted: always shown to a signed-in user. Reads what the caller
   * holds anywhere (ROOT plus every folder with a grant); baseline permissions that every
   * principal holds (`audit:list`, `group:list`, ...) never count, so an entry is shown only when
   * the caller can actually use the area.
   */
  visible?: (access: HeldAccess) => boolean;
};

export type NavSection = {
  id: string;
  /** Section label (not a heading); omitted for the first, unlabelled group. */
  label?: string;
  entries: NavEntry[];
};

const anyOf =
  (...permissions: string[]) =>
  (access: HeldAccess) =>
    holdsAny(access, ...permissions);

/** True when the caller holds any folder role or any ROOT-granted permission. */
const hasAnyAccess = ({ me }: HeldAccess) =>
  me.assignments.length > 0 || grantedGlobalPermissions(me).length > 0;

export const NAV_SECTIONS: NavSection[] = [
  {
    id: "main",
    label: "Overview",
    entries: [{ id: "home", label: "Home", to: "/", icon: Home }],
  },
  {
    id: "explorer",
    label: "Explorer",
    entries: [
      { id: "folders", label: "Folders", to: "/folders", icon: FolderTree },
      {
        id: "contracts",
        label: "Contracts",
        to: "/contracts",
        icon: FileText,
        visible: anyOf(
          "contract:view",
          "contract:read",
          "contract:read-published",
          "contract:read-metadata",
        ),
      },
      {
        id: "templates",
        label: "Templates",
        to: "/templates",
        icon: LayoutTemplate,
        visible: anyOf("template:read", "template:read-published"),
      },
    ],
  },
  {
    id: "governance",
    label: "Governance",
    entries: [
      {
        id: "rule-sets",
        label: "Rule sets",
        to: "/rule-sets",
        icon: ListChecks,
        visible: anyOf("ruleset:read", "ruleset:read-published"),
      },
      {
        id: "dry-run",
        label: "Dry runs",
        to: "/governance/dry-run",
        icon: FlaskConical,
        visible: anyOf("ruleset:dry-run"),
      },
      {
        id: "drift",
        label: "Drift",
        to: "/governance/drift",
        icon: Activity,
        visible: anyOf("governance:drift-read"),
      },
    ],
  },
  {
    id: "subscriptions",
    label: "Subscriptions",
    entries: [
      {
        id: "cards",
        label: "Subscriptions",
        to: "/cards",
        icon: Bell,
        visible: hasAnyAccess,
      },
    ],
  },
  {
    id: "audit",
    label: "Audit",
    entries: [
      {
        id: "audit",
        label: "Audit log",
        to: "/audit",
        icon: ScrollText,
        visible: anyOf("audit:query", "audit:read"),
      },
    ],
  },
  {
    id: "admin",
    label: "Admin",
    entries: [
      {
        id: "users",
        label: "Users",
        to: "/admin/users",
        icon: Users,
        visible: anyOf("user:read", "user:create"),
      },
      {
        id: "groups",
        label: "Groups",
        to: "/admin/groups",
        icon: UsersRound,
        visible: anyOf(
          "group:read",
          "group:create",
          "group:manage",
          "entra:read",
          "entra:manage",
        ),
      },
      {
        id: "service-accounts",
        label: "Service accounts",
        to: "/admin/service-accounts",
        icon: UserCog,
        visible: anyOf(
          "service-account:read",
          "service-account:create",
          "service-account:manage",
          "entra:read",
          "entra:manage",
        ),
      },
      {
        id: "pats",
        label: "Access tokens",
        to: "/admin/pats",
        icon: KeyRound,
        visible: anyOf("pat:read-all", "pat:revoke-any"),
      },
      {
        id: "settings",
        label: "Settings",
        to: "/admin/settings",
        icon: Settings,
        visible: anyOf("settings:read"),
      },
    ],
  },
];

/** The sections and entries this principal sees; empty sections are dropped. */
export function visibleSections(
  access: HeldAccess | undefined,
  sections: NavSection[] = NAV_SECTIONS,
): NavSection[] {
  return sections
    .map((section) => ({
      ...section,
      entries: section.entries.filter(
        (entry) =>
          entry.visible === undefined ||
          (access !== undefined && entry.visible(access)),
      ),
    }))
    .filter((section) => section.entries.length > 0);
}

/** Whether `pathname` is inside the entry's route. */
export function isCurrent(
  entry: Pick<NavEntry, "to">,
  pathname: string,
): boolean {
  if (entry.to === "/") return pathname === "/";
  return pathname === entry.to || pathname.startsWith(`${entry.to}/`);
}
