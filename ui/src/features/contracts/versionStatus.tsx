// Version status badges (D-UI-10-01): always a text label plus an icon, never colour alone.
// Deprecation is orthogonal to the status, so a version can be both Latest and Deprecated.
import {
  AlertTriangle,
  CheckCircle2,
  EyeOff,
  FilePen,
  FlaskConical,
  History,
  type LucideIcon,
} from "lucide-react";
import type { ContractSummary, ContractVersion } from "@/api/types";
import { Badge, type BadgeIntent } from "@/components/Badge";

export type VersionBadge = {
  key: string;
  label: string;
  intent: BadgeIntent;
  icon: LucideIcon;
};

// A version from the versions API, or the head summary of a contract list row (headVersionLike):
// a draft has no status of its own, so `lifecycle` also admits "draft" and `status` may be null.
type VersionLike = {
  status: ContractVersion["status"] | null;
  lifecycle: ContractVersion["lifecycle"] | "draft";
  deprecated: boolean;
  sunset_date: string | null;
};

export function versionBadges(version: VersionLike): VersionBadge[] {
  const out: VersionBadge[] = [];
  if (version.lifecycle === "draft") {
    out.push({
      key: "draft",
      label: "Draft",
      intent: "neutral",
      icon: FilePen,
    });
  } else if (version.lifecycle === "unpublished") {
    out.push({
      key: "unpublished",
      label: "Unpublished",
      intent: "neutral",
      icon: EyeOff,
    });
  } else if (version.status === "latest") {
    out.push({
      key: "latest",
      label: "Latest",
      intent: "success",
      icon: CheckCircle2,
    });
  } else if (version.status === "preview") {
    out.push({
      key: "preview",
      label: "Preview",
      intent: "brand",
      icon: FlaskConical,
    });
  } else {
    out.push({
      key: "older",
      label: "Older",
      intent: "neutral",
      icon: History,
    });
  }
  if (version.deprecated) {
    out.push({
      key: "deprecated",
      label: version.sunset_date
        ? `Deprecated · sunset ${version.sunset_date}`
        : "Deprecated",
      intent: "warning",
      icon: AlertTriangle,
    });
  }
  return out;
}

/**
 * A contract list row's head version as a VersionLike (D-08): a draft head is Draft, an unpublished
 * head Unpublished, any other head is published, Latest when the contract has a Latest version and
 * Preview when it has none. The row carries no sunset date, so Deprecated is the plain label.
 */
export function headVersionLike(
  row: Pick<ContractSummary, "version" | "latest_version">,
): VersionLike {
  const { state, deprecated } = row.version;
  if (state === "draft")
    return { lifecycle: "draft", status: null, deprecated, sunset_date: null };
  if (state === "unpublished")
    return {
      lifecycle: "unpublished",
      status: null,
      deprecated,
      sunset_date: null,
    };
  return {
    lifecycle: "published",
    status: row.latest_version ? "latest" : "preview",
    deprecated,
    sunset_date: null,
  };
}

/** The badges of a contract list row's head: the one lifecycle mapping through headVersionLike. */
export function headBadges(
  row: Pick<ContractSummary, "version" | "latest_version">,
): VersionBadge[] {
  return versionBadges(headVersionLike(row));
}

export function VersionBadges({ version }: { version: VersionLike }) {
  return (
    <>
      {versionBadges(version).map((badge) => (
        <Badge key={badge.key} intent={badge.intent} icon={badge.icon}>
          {badge.label}
        </Badge>
      ))}
    </>
  );
}
