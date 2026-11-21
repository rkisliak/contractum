// Sentence-case labels for the API's role ids (UI-SPEC 7.5). Unknown ids fall back to the id
// with underscores as spaces, so a new backend role still reads sensibly.
const ROLE_LABELS: Record<string, string> = {
  global_admin: "Global admin",
  global_reader: "Global reader",
  org_admin: "Organization admin",
  org_reader: "Organization reader",
  project_admin: "Project admin",
  project_editor: "Project editor",
  team_admin: "Team admin",
  team_editor: "Team editor",
  team_reader: "Team reader",
  policy_manager: "Policy manager",
  consumer: "Consumer",
  notifier: "Notifier",
};

// One plain sentence per role: what holding it means (copy only, never permissions). Shared by
// the grant dialog and the "Your effective access" summary.
const ROLE_MEANINGS: Record<string, string> = {
  global_admin: "Everything, everywhere, including users and global settings.",
  global_reader: "Reads every folder, contract and setting.",
  org_admin: "Manages this organization: folders, access, groups and accounts.",
  org_reader: "Reads everything in this organization.",
  project_admin: "Manages this project, its teams and their access.",
  project_editor: "Edits contracts in every team of this project.",
  team_admin: "Manages this team, its contracts and its access.",
  team_editor: "Creates, edits and publishes contracts in this team.",
  team_reader: "Reads this team's contracts.",
  policy_manager: "Authors rule sets and templates here and below.",
  consumer: "Fetches published contracts here and below.",
  notifier:
    "Service role of the built-in notifier account: sends notice emails and copies webhook hosts to the allowed-hosts list once. Never grant it to people.",
};

/** What holding the role means, in one sentence ("" for an unknown role). */
export function roleMeaning(role: string): string {
  return ROLE_MEANINGS[role] ?? "";
}

export function roleLabel(role: string): string {
  const known = ROLE_LABELS[role];
  if (known !== undefined) return known;
  const words = role.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// Folder kinds in the fixed hierarchy (ROOT, Organization, Project, Team).
const KIND_LABELS: Record<string, string> = {
  root: "Root",
  organization: "Organization",
  project: "Project",
  team: "Team",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

/** The kind a child of `kind` gets, or null when the kind takes no children (Team). */
export function childKindOf(kind: string): string | null {
  switch (kind) {
    case "root":
      return "organization";
    case "organization":
      return "project";
    case "project":
      return "team";
    default:
      return null;
  }
}
