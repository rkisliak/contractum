// The platform announcement (Admin > Settings > Platform, quick 261120-0yc, P1). Shown above the
// page content to every signed-in user while it is active, as plain text only (React text nodes:
// never HTML or markdown) in an info or warning Alert. A person can dismiss it for the browser
// session; the key changes when the text, severity or start changes, so a new announcement shows
// again. It shows nothing when there is no banner or the request failed.
import { useState } from "react";
import { Alert } from "@/components/Alert";
import { usePublicSettings } from "@/features/settings/api";

const DISMISS_PREFIX = "dc-banner-dismissed:";

type BannerLike = {
  text: string;
  severity: "info" | "warning";
  starts_at?: string | null;
};

/** A stable key of the announcement (a 32-bit string hash; not a security control). */
export function bannerKey(banner: BannerLike): string {
  const source = `${banner.severity}|${banner.starts_at ?? ""}|${banner.text}`;
  let hash = 5381;
  for (let i = 0; i < source.length; i += 1) {
    // hash * 33 + c modulo 2^32: the same residue the int32 wrap gave, kept as an unsigned
    // integer (always below 2^53, so exact). Math.trunc would drop the intended 32-bit wrap.
    hash = (hash * 33 + source.charCodeAt(i)) >>> 0;
  }
  return hash.toString(16);
}

function isDismissed(key: string): boolean {
  try {
    return window.sessionStorage.getItem(DISMISS_PREFIX + key) === "1";
  } catch {
    return false;
  }
}

function remember(key: string): void {
  try {
    window.sessionStorage.setItem(DISMISS_PREFIX + key, "1");
  } catch {
    // Storage blocked: the banner just comes back on the next page load.
  }
}

/** The banner as drawn (also the live preview in Settings). */
export function AnnouncementView({
  banner,
  onDismiss,
}: {
  banner: BannerLike;
  onDismiss?: () => void;
}) {
  return (
    <Alert
      intent={banner.severity === "warning" ? "warning" : "info"}
      title="Announcement"
      onDismiss={onDismiss}
    >
      {banner.text}
    </Alert>
  );
}

export function AnnouncementBanner() {
  const settings = usePublicSettings();
  const [dismissed, setDismissed] = useState<string | null>(null);
  const banner = settings.data?.banner ?? null;
  if (banner === null) return null;
  const key = bannerKey(banner);
  if (dismissed === key || isDismissed(key)) return null;
  return (
    <AnnouncementView
      banner={banner}
      onDismiss={() => {
        remember(key);
        setDismissed(key);
      }}
    />
  );
}
