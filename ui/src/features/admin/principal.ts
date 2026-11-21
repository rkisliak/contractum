// Principal ids in the documented shapes (mirrors identity's PRINCIPAL_ID_PATTERNS, D-UI-12-08):
// the picker builds them from records, so an admin never types `local:<uuid>`. Labels for the
// principal types and a one-line description per role (copy only, never permissions).
import type { GrantCreate } from "@/api/types";
import { roleMeaning } from "@/features/access/roles";

export type PrincipalType = GrantCreate["principal_type"];

const GUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";

export const PRINCIPAL_ID_PATTERNS: Record<PrincipalType, RegExp> = {
  user: /^[a-z][a-z0-9_-]*:[A-Za-z0-9._:-]+$/,
  local_group: new RegExp(`^${GUID}$`),
  entra_group: new RegExp(`^${GUID}$`),
  service_principal: new RegExp(`^entra:${GUID}:${GUID}$`),
  service_account: new RegExp(`^sa:${GUID}$`),
};

export function isValidPrincipalId(type: PrincipalType, id: string): boolean {
  return PRINCIPAL_ID_PATTERNS[type].test(id);
}

/** The grant/member principal id for each record kind. */
export const principalIdOf = {
  localUser: (userId: string) => `local:${userId}`,
  entraUser: (tenantId: string, objectId: string) =>
    `entra:${tenantId}:${objectId}`,
  localGroup: (groupId: string) => groupId.toLowerCase(),
  entraGroup: (objectId: string) => objectId.toLowerCase(),
  servicePrincipal: (tenantId: string, objectId: string) =>
    `entra:${tenantId}:${objectId}`,
  serviceAccount: (accountId: string) => `sa:${accountId}`,
};

export const PRINCIPAL_TYPE_LABELS: Record<PrincipalType, string> = {
  user: "User",
  local_group: "Local group",
  entra_group: "Entra group",
  service_account: "Service account",
  service_principal: "Entra service principal",
};

/**
 * A principal type in plain words, for the type alone or with the principal id when it tells more:
 * a user is a "Local user" (`local:...`) or an "Entra user" (`entra:...`). Principals from
 * `/me` use `entra_user`; anything unknown reads as its words ("some_type" becomes "Some type").
 */
export function principalTypeLabel(
  type: string,
  principalId?: string | null,
): string {
  if (type === "user") {
    if (principalId?.startsWith("entra:")) return "Entra user";
    if (principalId?.startsWith("local:")) return "Local user";
    return PRINCIPAL_TYPE_LABELS.user;
  }
  if (type === "entra_user") return "Entra user";
  if (type in PRINCIPAL_TYPE_LABELS)
    return PRINCIPAL_TYPE_LABELS[type as PrincipalType];
  const words = type.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function roleDescription(role: string): string {
  return roleMeaning(role);
}
