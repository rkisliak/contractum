// What the layout audit (`layout.spec.ts`) must reach. Plain data, no Playwright import, so the
// Vitest guard `src/test/layoutCoverage.test.ts` can compare it with the route files and the
// overlay components in `src/`: a new route file, or a new component that renders a Dialog,
// ConfirmDialog, SecretRevealDialog, Drawer or Menu, fails `pnpm test` until it is listed here,
// and the e2e run fails when a listed route is not visited or a listed overlay is not opened.
import type { Demo } from "./journey-helpers";
import type { Volume } from "./volume-seed";

/** Route file (relative to src/routes/_app, without .tsx) -> the URLs that exercise it. */
export const ROUTES: Record<string, (d: Demo) => (string | null)[]> = {
  index: () => ["/"],
  "account/profile": () => ["/account/profile"],
  "account/password": () => ["/account/password"],
  "account/tokens": () => ["/account/tokens"],
  // Redirects to the merged Service accounts page, which proves the redirect lands on a clean page.
  "admin/entra/index": () => ["/admin/entra?tab=service-principals"],
  "admin/entra/users/$entraUserId": (d) => [
    d.entraUserId && `/admin/entra/users/${d.entraUserId}`,
  ],
  "admin/entra/groups/$registrationId": (d) => [
    d.entraGroupId && `/admin/entra/groups/${d.entraGroupId}`,
  ],
  "admin/entra/service-principals/$registrationId": (d) => [
    d.entraSpId && `/admin/entra/service-principals/${d.entraSpId}`,
  ],
  "admin/groups/index": () => ["/admin/groups", "/admin/groups?source=entra"],
  "admin/groups/$groupId": (d) => [d.groupId && `/admin/groups/${d.groupId}`],
  "admin/pats/index": () => ["/admin/pats"],
  "admin/service-accounts/index": () => [
    "/admin/service-accounts",
    "/admin/service-accounts?source=entra",
  ],
  "admin/service-accounts/$serviceAccountId": (d) => [
    d.serviceAccountId && `/admin/service-accounts/${d.serviceAccountId}`,
  ],
  "admin/settings/index": () => [
    "/admin/settings?tab=entra",
    "/admin/settings?tab=notifications",
    "/admin/settings?tab=governance",
    "/admin/settings?tab=security",
    "/admin/settings?tab=platform",
  ],
  "admin/users/index": () => ["/admin/users", "/admin/users?source=entra"],
  "admin/users/$userId": (d) => [`/admin/users/${d.userId}`],
  "audit/index": () => ["/audit"],
  "cards/index": (d) => ["/cards", `/cards?folder=${d.fraud.id}`],
  "cards/$cardId": (d) =>
    ["overview", "members", "subscriptions", "notices", "webhook"].map(
      (tab) => d.card && `/cards/${d.card.id}?tab=${tab}`,
    ),
  "contracts/index": () => ["/contracts", "/contracts?q=zz-no-such-contract"],
  "contracts/new": (d) => [
    "/contracts/new",
    `/contracts/new?folder=${d.ledger.id}`,
  ],
  "contracts/$contractId/index": (d) => [`/contracts/${d.orders.id}`],
  "contracts/$contractId/document": (d) => [
    `/contracts/${d.orders.id}/document`,
  ],
  "contracts/$contractId/drafts/index": (d) => [
    `/contracts/${d.orders.id}/drafts`,
  ],
  "contracts/$contractId/drafts/$draftId": (d) => [
    d.draftId && `/contracts/${d.orders.id}/drafts/${d.draftId}`,
  ],
  "contracts/$contractId/versions/index": (d) => [
    `/contracts/${d.orders.id}/versions`,
  ],
  "contracts/$contractId/versions/$ref": (d) => [
    `/contracts/${d.orders.id}/versions/1.0.0`,
  ],
  "contracts/$contractId/diff": (d) => [
    `/contracts/${d.orders.id}/diff`,
    `/contracts/${d.orders.id}/diff?from=1.0.0&to=1.1.0`,
  ],
  "contracts/$contractId/rule-sets": (d) => [
    `/contracts/${d.orders.id}/rule-sets`,
  ],
  "contracts/$contractId/subscribers": (d) => [
    `/contracts/${d.orders.id}/subscribers`,
  ],
  "folders/index": () => ["/folders"],
  "folders/$folderId/index": (d) => [
    `/folders/${d.root.id}`,
    `/folders/${d.acme.id}`,
    `/folders/${d.ledger.id}`,
  ],
  "folders/$folderId/contracts": (d) => [`/folders/${d.ledger.id}/contracts`],
  "folders/$folderId/folders": (d) => [`/folders/${d.payments.id}/folders`],
  "folders/$folderId/templates": (d) => [`/folders/${d.acme.id}/templates`],
  "folders/$folderId/rule-sets": (d) => [`/folders/${d.acme.id}/rule-sets`],
  "folders/$folderId/cards": (d) => [`/folders/${d.fraud.id}/cards`],
  "folders/$folderId/access": (d) => [`/folders/${d.ledger.id}/access`],
  "governance/dry-run": (d) => [
    "/governance/dry-run",
    `/governance/dry-run?rule_set_id=${d.ruleSet.id}&version=1&folder_id=${d.acme.id}`,
  ],
  "governance/drift": (d) => [
    "/governance/drift",
    `/governance/drift?folder_id=${d.acme.id}`,
  ],
  "rule-sets/index": (d) => ["/rule-sets", `/rule-sets?folder_id=${d.acme.id}`],
  "rule-sets/$ruleSetId": (d) =>
    ["draft", "versions", "rollout", "impact"].map(
      (tab) => `/rule-sets/${d.ruleSet.id}?tab=${tab}`,
    ),
  "templates/index": () => ["/templates"],
  "templates/$templateId": (d) => [`/templates/${d.template.id}`],
};

/** Route files outside the signed-in shell that the audit covers or deliberately leaves out. */
export const PUBLIC_ROUTES: Record<string, string> = {
  signin: "checked signed out in layout.spec.ts",
  "signin_.rotate":
    "needs the one-time bootstrap rotation token; covered by RotatePage.test.tsx and global-setup on a cold stack",
  "auth.callback": "the Entra redirect target; covered by e2e/entra.spec.ts",
};

/**
 * Overlay component file (relative to src/) -> the title patterns of the overlays it renders.
 * Every pattern must match the accessible name of at least one dialog, drawer or menu the audit
 * opened (menus are named by their trigger).
 */
export const OVERLAYS: Record<string, RegExp[]> = {
  "features/account/SessionsCard.tsx": [
    /^Sign out this session\?$/,
    /^Sign out every other session\?$|^Sign out all of .+ sessions\?$/,
  ],
  "features/admin/CreateCredentialDialog.tsx": [/^Create (token|key)$/],
  "features/admin/CreateOwnedDialog.tsx": [
    /^Create local group$/,
    /^Create service account$/,
  ],
  "features/admin/EntraDetailPages.tsx": [
    /^(Deactivate|Reactivate) .+\?$|^Delete the registration of /,
  ],
  "features/admin/EntraRegisterDialog.tsx": [
    /^Register Entra (user|group|service principal)$/,
  ],
  "features/admin/FolderAccess.tsx": [/^Grant access$/, /inheritance at /],
  "features/admin/GroupDetailPage.tsx": [/^Add a member to /, /^Rename /],
  "features/admin/ServiceAccountDetailPage.tsx": [
    /^Create key$/,
    /^Revoke .+\?$/,
  ],
  "features/settings/ImportSettingsDialog.tsx": [/^Import settings$/],
  "features/settings/SendTestEmailDialog.tsx": [/^Send a test email$/],
  "features/admin/SourceList.tsx": [/^menu New$/],
  "features/admin/TokensPages.tsx": [/^Your new /, /^Revoke .+\?$/],
  "features/admin/UserDetailPage.tsx": [
    /^(Deactivate|Reactivate) admin\?$/,
    /^Edit display name$/,
  ],
  "features/admin/UsersPage.tsx": [/^Create local user$/],
  "features/audit/AuditEventDrawer.tsx": [/^Audit event$/],
  "features/cards/CardFormDialog.tsx": [/^Create card$/, /^Edit card$/],
  "features/cards/CardMembers.tsx": [/^Add member$/],
  "features/cards/CardPage.tsx": [/^Archive .+\?$/],
  "features/cards/CardSubscriptions.tsx": [/^Unsubscribe\?$/],
  "features/cards/CardWebhook.tsx": [/^Rotate the webhook secret\?$/],
  "features/cards/SubscribeDialog.tsx": [
    /^Subscribe to a contract$/,
    /^Move pin$/,
  ],
  "features/contracts/ContractDrafts.tsx": [/^Create a draft$/],
  "features/contracts/ContractOverview.tsx": [/^Delete .+\?$/],
  "features/contracts/ContractRuleSets.tsx": [/^Attach a rule set$/],
  "features/contracts/ContractSubscribers.tsx": [/^Remove .+ from .+\?$/],
  "features/contracts/DraftEditor.tsx": [/^Discard draft/],
  "features/contracts/PublishDialog.tsx": [/^Publish .+\?$/],
  "features/contracts/VersionActions.tsx": [/^menu Actions for \d/],
  "features/editor/EditorDialog.tsx": [/ · Document$/, / · Draft( |$)/],
  "features/folders/CreateFolderDialog.tsx": [
    /^Create (organization|project|team)$/,
  ],
  "features/folders/FolderHeader.tsx": [
    /^menu Actions for [A-Z]/,
    /^Archive .+\?$/,
  ],
  "features/folders/RenameFolderDialog.tsx": [/^Rename /],
  "features/rule-sets/CreateRuleSetDialog.tsx": [/^Create rule set$/],
  "features/rule-sets/RuleDraftEditor.tsx": [
    /^Edit draft as JSON$/,
    /^Remove rule /,
  ],
  "features/rule-sets/RuleSetRollout.tsx": [
    /^(Make .+ mandatory|Stop requiring .+|Enforce version .+ now)\?$/,
  ],
  "features/rule-sets/RuleSetVersions.tsx": [
    /^Version \d+ of /,
    /^Deprecate version /,
  ],
  "features/shell/AccountMenu.tsx": [/^menu account$/],
  "features/shell/AppShell.tsx": [/^Navigation$/],
  "features/shell/VersionsDialog.tsx": [/^Versions$/],
  "features/templates/CreateTemplateDialog.tsx": [/^Create template$/],
  "features/templates/TemplatePage.tsx": [/^Publish template version /],
};

const LONG_Q = `q=${"verylongsearchtext".repeat(11).slice(0, 190)}`;

/**
 * The volume audit (`volume.spec.ts`, quick 261120-6i0): route file -> the URLs that exercise it
 * over the volume dataset. The keys equal `ROUTES` exactly (`layoutCoverage.test.ts` enforces it),
 * so a new route needs URLs here too.
 */
export const VOLUME_ROUTES: Record<string, (v: Volume) => (string | null)[]> = {
  index: () => ["/"],
  "account/profile": () => ["/account/profile"],
  "account/password": () => ["/account/password"],
  "account/tokens": () => ["/account/tokens"],
  "admin/entra/index": () => ["/admin/entra?tab=service-principals"],
  "admin/entra/users/$entraUserId": (v) => [
    `/admin/entra/users/${v.entraUser.id}`,
  ],
  "admin/entra/groups/$registrationId": (v) => [
    `/admin/entra/groups/${v.entraGroup.id}`,
    `/admin/entra/groups/${v.entraBigGroup.id}`,
  ],
  "admin/entra/service-principals/$registrationId": (v) => [
    `/admin/entra/service-principals/${v.entraSp.id}`,
  ],
  "admin/groups/index": () => ["/admin/groups", "/admin/groups?source=entra"],
  "admin/groups/$groupId": (v) => [`/admin/groups/${v.group120.id}`],
  "admin/pats/index": () => ["/admin/pats"],
  "admin/service-accounts/index": () => [
    "/admin/service-accounts",
    "/admin/service-accounts?source=entra",
  ],
  "admin/service-accounts/$serviceAccountId": (v) => [
    `/admin/service-accounts/${v.serviceAccount.id}`,
    `/admin/service-accounts/${v.serviceAccountLong.id}`,
  ],
  "admin/settings/index": () => [
    "/admin/settings?tab=entra",
    "/admin/settings?tab=notifications",
    "/admin/settings?tab=governance",
    "/admin/settings?tab=security",
    "/admin/settings?tab=platform",
  ],
  "admin/users/index": () => ["/admin/users", "/admin/users?source=entra"],
  "admin/users/$userId": (v) => [
    `/admin/users/${v.longEmailUser.id}`,
    `/admin/users/${v.wanderer.id}`,
    `/admin/users/${v.adminUser.id}`,
  ],
  "audit/index": () => ["/audit"],
  "cards/index": (v) => [
    "/cards",
    `/cards?folder=${v.longTeam.id}`,
    `/cards?${LONG_Q}`,
  ],
  "cards/$cardId": (v) =>
    ["overview", "members", "subscriptions", "notices", "webhook"].map(
      (tab) => `/cards/${v.card20.id}?tab=${tab}`,
    ),
  "contracts/index": () => ["/contracts", `/contracts?${LONG_Q}`],
  "contracts/new": (v) => [
    "/contracts/new",
    `/contracts/new?folder=${v.longTeam.id}`,
  ],
  "contracts/$contractId/index": (v) => [
    `/contracts/${v.bigContract.id}`,
    `/contracts/${v.longIdContract.id}`,
  ],
  "contracts/$contractId/document": (v) => [
    `/contracts/${v.bigContract.id}/document`,
    `/contracts/${v.longIdContract.id}/document`,
  ],
  "contracts/$contractId/drafts/index": (v) => [
    `/contracts/${v.draftContract.id}/drafts`,
  ],
  "contracts/$contractId/drafts/$draftId": (v) => [
    `/contracts/${v.draftContract.id}/drafts/${v.draftContract.draftId}`,
  ],
  "contracts/$contractId/versions/index": (v) => [
    `/contracts/${v.bigContract.id}/versions`,
  ],
  "contracts/$contractId/versions/$ref": (v) => [
    `/contracts/${v.bigContract.id}/versions/1.20.0`,
    `/contracts/${v.bigContract.id}/versions/1.3.0`,
    `/contracts/${v.bigContract.id}/versions/1.24.0`,
  ],
  "contracts/$contractId/diff": (v) => [
    `/contracts/${v.bigContract.id}/diff`,
    `/contracts/${v.bigContract.id}/diff?from=1.0.0&to=1.24.0`,
  ],
  "contracts/$contractId/rule-sets": (v) => [
    `/contracts/${v.bigContract.id}/rule-sets`,
  ],
  "contracts/$contractId/subscribers": (v) => [
    `/contracts/${v.bigContract.id}/subscribers`,
  ],
  "folders/index": () => ["/folders"],
  "folders/$folderId/index": (v) => [
    `/folders/${v.longTeam.id}`,
    `/folders/${v.rtlTeam.id}`,
    `/folders/${v.accessTeam.id}`,
    `/folders/${v.orgs[0]?.id ?? ""}`,
  ],
  "folders/$folderId/contracts": (v) => [
    `/folders/${v.longTeam.id}/contracts`,
    `/folders/${v.longTeam.id}/contracts?${LONG_Q}`,
  ],
  "folders/$folderId/folders": (v) => [
    `/folders/${v.orgs[0]?.id ?? ""}/folders`,
    `/folders/${v.folders["vol-o1-p1"]?.id ?? ""}/folders`,
  ],
  "folders/$folderId/templates": (v) => [
    `/folders/${v.orgs[0]?.id ?? ""}/templates`,
  ],
  "folders/$folderId/rule-sets": (v) => [`/folders/${v.longTeam.id}/rule-sets`],
  "folders/$folderId/cards": (v) => [
    `/folders/${v.longTeam.id}/cards`,
    `/folders/${v.longTeam.id}/cards?${LONG_Q}`,
  ],
  "folders/$folderId/access": (v) => [
    `/folders/${v.accessTeam.id}/access`,
    `/folders/${v.longTeam.id}/access`,
  ],
  "governance/dry-run": (v) => [
    "/governance/dry-run",
    `/governance/dry-run?rule_set_id=${v.ruleSet20.id}&version=20&folder_id=${v.longTeam.id}`,
  ],
  "governance/drift": (v) => [
    "/governance/drift",
    `/governance/drift?folder_id=${v.orgs[0]?.id ?? ""}`,
  ],
  "rule-sets/index": (v) => [
    "/rule-sets",
    `/rule-sets?folder_id=${v.longTeam.id}`,
  ],
  "rule-sets/$ruleSetId": (v) =>
    ["draft", "versions", "rollout", "impact"].map(
      (tab) => `/rule-sets/${v.ruleSet20.id}?tab=${tab}`,
    ),
  "templates/index": () => ["/templates"],
  "templates/$templateId": (v) => [`/templates/${v.longTemplate.id}`],
};

/**
 * Source file (relative to src/) -> the accessible-name patterns of the searchable popups it
 * renders (a `Combobox` through `PrincipalPicker` or `EntraDirectorySearch`) and of the
 * "Show access at" select. The volume audit types into each and asserts the capped listbox
 * (at most 20 options and "Type more to narrow the list"); `layoutCoverage.test.ts` fails when a
 * file that renders `<Combobox`, `<PrincipalPicker` or `<EntraDirectorySearch` is missing here.
 */
export const POPUPS: Record<string, RegExp[]> = {
  "features/admin/EntraRegisterDialog.tsx": [
    /^Search Entra (users|groups|service principals)$/,
  ],
  "features/admin/FolderAccess.tsx": [/^Search (users|groups|.+)s?$/],
  "features/admin/GroupDetailPage.tsx": [/^Search (users|groups|.+)s?$/],
  "features/cards/CardMembers.tsx": [/^Search (users|groups|.+)s?$/],
  "features/access/EffectiveAccess.tsx": [/^Show access at$/],
};
