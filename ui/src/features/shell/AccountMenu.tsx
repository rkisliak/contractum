// Account menu (UI-SPEC section 6): an avatar button at the bottom of the icon rail (1024px and
// up; the menu opens to the right of the rail) or at the end of the compact top bar (below
// 1024px). The menu header shows the name, "Role · Folder" (the folder where the role is held) and
// the sign-in method; then "Account settings", "Access tokens", "Versions" and "Sign out".
import { useRouteContext } from "@tanstack/react-router";
import { Info, KeyRound, LogOut, Settings } from "lucide-react";
import { Avatar } from "@/components/Avatar";
import { StatusBadge } from "@/components/Badge/status";
import { Menu } from "@/components/Menu";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { roleLabel } from "@/features/access/roles";
import { useMe } from "@/features/access/useMe";
import { useFolderList } from "@/features/folders/api";
import { cx } from "@/lib/cx";
import styles from "./AccountMenu.module.css";

export function AccountMenu({
  onSignOutFailed,
  onShowVersions,
  variant = "topbar",
}: {
  onSignOutFailed: () => void;
  /** Opens the "Versions" dialog (also the rail's info button). */
  onShowVersions: () => void;
  /** "rail": the slate icon rail (menu to the right); "topbar": the compact top bar. */
  variant?: "rail" | "topbar";
}) {
  const { session } = useRouteContext({ from: "__root__" });
  const me = useMe();
  const folders = useFolderList();
  const name = me.data?.display_name ?? "your account";
  const top = me.data?.assignments[0];
  const role = top ? roleLabel(top.role) : "No folder role";
  // The folder where the role is held (its display name, else the last path segment).
  const scope = top
    ? top.folder_path === ""
      ? "Root"
      : (folders.data?.folders.find((folder) => folder.id === top.folder_id)
          ?.display_name ??
        top.folder_path.split("/").at(-1) ??
        "")
    : "";
  const roleLine = scope ? `${role} · ${scope}` : role;

  const trigger = (
    <button
      type="button"
      className={cx(styles.trigger, variant === "rail" && styles.rail)}
    >
      <Avatar name={me.data?.display_name ?? ""} className={styles.avatar} />
      {/* Named by text, not aria-label: the visible initials are decorative (axe
          label-content-name-mismatch). */}
      <VisuallyHidden>{`Account menu for ${name}`}</VisuallyHidden>
    </button>
  );

  return (
    <Menu
      trigger={trigger}
      {...(variant === "rail" ? { tooltip: name } : {})}
      side={variant === "rail" ? "right" : "bottom"}
      align="end"
      header={
        me.data ? (
          <>
            <span className={styles.name}>{me.data.display_name}</span>
            <span className={styles.meta}>{roleLine}</span>
            <span>
              <StatusBadge
                status={me.data.idp === "entra" ? "entra" : "local"}
              />
            </span>
          </>
        ) : undefined
      }
      items={[
        { label: "Account settings", icon: Settings, to: "/account/profile" },
        { label: "Access tokens", icon: KeyRound, to: "/account/tokens" },
        { label: "Versions", icon: Info, onSelect: onShowVersions },
        {
          label: "Sign out",
          icon: LogOut,
          onSelect: () => {
            session.signOut().catch(onSignOutFailed);
          },
        },
      ]}
    />
  );
}
