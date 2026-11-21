// Shared StatusBadge intent map (UI-SPEC 5.7). Later phases add rows here (lifecycle, kind,
// mandatory and enforced labels), never new styling. Icons come with the row so a screen cannot
// pair the wrong glyph with a status. This file stays `.ts` (the plan's path), so the component
// is built with createElement rather than JSX.
import type { LucideIcon } from "lucide-react";
import { Building2, User } from "lucide-react";
import { createElement, type ReactElement } from "react";
import { Badge, type BadgeIntent } from "./index";

export type StatusBadgeSpec = {
  intent: BadgeIntent;
  icon: LucideIcon;
  label: string;
};

export const statusBadges = {
  local: { intent: "neutral", icon: User, label: "Local account" },
  entra: { intent: "brand", icon: Building2, label: "Microsoft Entra" },
  // Where a principal record comes from, in the admin lists and detail headers (quick 261119-l30).
  "source-local": { intent: "neutral", icon: User, label: "Local" },
  "source-entra": { intent: "brand", icon: Building2, label: "Entra" },
} as const satisfies Record<string, StatusBadgeSpec>;

export type StatusBadgeKey = keyof typeof statusBadges;

export function StatusBadge({
  status,
}: {
  status: StatusBadgeKey;
}): ReactElement {
  const spec: StatusBadgeSpec = statusBadges[status];
  return createElement(Badge, {
    intent: spec.intent,
    icon: spec.icon,
    children: spec.label,
  });
}
