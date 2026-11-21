// Short names for the generated schema types that screens share. Never hand-write a response
// shape: alias it from `schema.gen.ts` here so a backend change still fails type checking.
import type { components } from "./schema.gen";

type Schemas = components["schemas"];

export type Me = Schemas["IdentityMeOut"];
export type MeAssignment = Schemas["IdentityMeAssignmentOut"];
export type MeGroup = Schemas["IdentityMeGroupOut"];
export type Capabilities = Schemas["IdentityMeCapabilitiesOut"];
export type SessionList = Schemas["IdentitySessionListOut"];
export type Session = Schemas["IdentitySessionOut"];
export type Folder = Schemas["IdentityFolderOut"];
export type FolderDeletionCheck = Schemas["IdentityFolderDeletionCheckOut"];
export type FolderBlocker = Schemas["IdentityFolderBlockerOut"];
export type FolderPurge = Schemas["IdentityFolderPurgeOut"];
export type ContractSummary = Schemas["ContractMetadataOut"];
export type ContractDraft = Schemas["ContractDraftOut"];
export type ContractDraftWrite = Schemas["ContractDraftWriteOut"];
export type ContractCreated = Schemas["ContractCreatedOut"];
export type ContractVersion = Schemas["ContractVersionMetadataOut"];
export type ContractVersionPublished = Schemas["ContractVersionPublishedOut"];
export type ContractDiff = Schemas["ContractDiffOut"];
export type ContractValidationReport = Schemas["ContractValidationReportOut"];
export type ContractDraftValidation = Schemas["ContractDraftValidationOut"];
export type ContractRuleSetAttachment = Schemas["ContractRuleSetAttachmentOut"];
export type ContractSubscribers = Schemas["ContractSubscribersOut"];
export type ContractSubscriberVersion = Schemas["ContractSubscriberVersionOut"];
export type ContractAvailableTemplate = Schemas["ContractAvailableTemplateOut"];
export type AvailableRuleSet = Schemas["AvailableRuleSetOut"];

// Governance (templates, rule sets, dry-run and drift).
export type Template = Schemas["ContractTemplateOut"];
export type TemplateVersion = Schemas["ContractTemplateVersionOut"];
export type TemplateWrite = Schemas["ContractTemplateWriteOut"];
export type AvailableTemplate = Schemas["ContractAvailableTemplateOut"];
export type StructuralReport = Schemas["ContractStructuralReportOut"];
export type RuleSet = Schemas["RuleSetOut"];
export type RuleSetDraft = Schemas["RuleSetDraftOut"];
export type RuleDef = Schemas["RuleDef"];
export type RuleSetVersion = Schemas["RuleSetVersionOut"];
export type RuleSetVersionSummary = Schemas["RuleSetVersionSummaryOut"];
export type ResolvedEntry = Schemas["ResolvedEntryOut"];
export type DryRunResult = Schemas["ContractsRuleSetDryRunOut"];
export type DryRunItem = Schemas["ContractsDryRunItemOut"];
export type DriftResult = Schemas["ContractsDriftOut"];
export type DriftItem = Schemas["ContractsDriftItemOut"];
export type ImpactFailingRule = Schemas["ContractsImpactFailingRuleOut"];
export type SubscriberCard = Schemas["SubscriberCardOut"];
export type SubscriberCardSummary = Schemas["SubscriberCardSummaryOut"];
export type SubscriberCardIndexItem = Schemas["SubscriberCardIndexItemOut"];
export type SubscriberCardIndex = Schemas["SubscriberCardIndexOut"];
export type CardContactIn = Schemas["CardContactIn"];
export type CardMember = Schemas["CardMemberOut"];
export type CardSubscription = Schemas["CardSubscriptionOut"];
export type CardNotice = Schemas["CardNoticeOut"];
export type AuditEvent = Schemas["AuditEventOut"];
// Access administration and identities (Phase 12).
export type LocalUser = Schemas["IdentityUserOut"];
export type LocalGroup = Schemas["IdentityGroupOut"];
export type GroupMember = Schemas["IdentityGroupMemberOut"];
export type GrantableGroup = Schemas["IdentityGrantableGroupOut"];
export type Grant = Schemas["IdentityGrantOut"];
export type GrantCreate = Schemas["IdentityGrantCreateIn"];
export type EffectiveAccess = Schemas["IdentityEffectiveAccessOut"];
export type EntraUser = Schemas["IdentityEntraUserOut"];
export type EntraGroup = Schemas["IdentityEntraGroupOut"];
export type EntraServicePrincipal = Schemas["IdentityEntraServicePrincipalOut"];
export type EntraGroupMember = Schemas["IdentityEntraGroupMemberOut"];
export type EntraDirectoryMatch = Schemas["IdentityEntraDirectoryMatchOut"];
export type EntraDirectorySearch = Schemas["IdentityEntraDirectorySearchOut"];
export type ServiceAccount = Schemas["IdentityServiceAccountOut"];
export type ServiceAccountKey = Schemas["IdentityServiceAccountKeyOut"];
export type Pat = Schemas["IdentityPatOut"];
export type AdminPat = Schemas["IdentityAdminPatOut"];
export type ServiceVersion = Schemas["ServiceVersionOut"];

// Admin > Settings (quick 261120-0yc).
export type SettingsOut = Schemas["IdentitySettingsOut"];
export type SettingsSection = Schemas["IdentitySettingsSectionOut"];
export type SettingsPublic = Schemas["IdentitySettingsPublicOut"];
export type SettingsBanner = Schemas["IdentityBannerOut"];
export type SettingsImport = Schemas["IdentitySettingsImportOut"];
export type SettingsChange = Schemas["IdentitySettingsChangeOut"];
export type EntraOverview = Schemas["IdentityEntraOverviewOut"];
export type EntraTest = Schemas["IdentityEntraTestOut"];
export type SettingsAbout = Schemas["IdentityAboutOut"];
export type SettingsAboutService = Schemas["IdentityAboutServiceOut"];
// Sign-in and security settings (quick 261120-0ye).
export type AuthPolicy = Schemas["IdentityAuthPolicyOut"];
export type PasswordRules = Schemas["IdentityPasswordRulesOut"];
// Notifications settings (quick 261120-0yd).
export type NotificationPreview = Schemas["IdentityNotificationPreviewOut"];
export type NotificationPreviewIn = Schemas["IdentityNotificationPreviewIn"];
export type TestEmail = Schemas["ContractsTestEmailOut"];
