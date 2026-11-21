// Version status badges (D-UI-10-01): always a text label plus an icon, never colour alone.
// Deprecation is orthogonal to the status, so a version can be both Latest and Deprecated.
import {
  AlertTriangle,
  CheckCircle2,
  EyeOff,
  FlaskConical,
  History,
  type LucideIcon,
} from "lucide-react";
import type { ContractVersion } from "@/api/types";
import { Badge, type BadgeIntent } from "@/components/Badge";

export type VersionBadge = {
  key: string;
  label: string;
  intent: BadgeIntent;
  icon: LucideIcon;
};

type VersionLike = Pick<
  ContractVersion,
  "status" | "lifecycle" | "deprecated" | "sunset_date"
>;

export function versionBadges(version: VersionLike): VersionBadge[] {
  const out: VersionBadge[] = [];
  if (version.lifecycle === "unpublished") {
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
