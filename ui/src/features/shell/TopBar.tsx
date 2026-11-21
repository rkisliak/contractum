// The compact top bar, shown under 1024px only (the icon rail and the navigation panel replace it
// on wide screens): the navigation toggle that opens the start drawer, the product mark and, at
// the end, the account menu.
import { LogoMark, Wordmark } from "@/components/Logo";
import { Link } from "@tanstack/react-router";
import { Menu as MenuIcon } from "lucide-react";
import { IconButton } from "@/components/Button";
import { AccountMenu } from "./AccountMenu";
import styles from "./TopBar.module.css";

export function TopBar({
  drawerOpen,
  onOpenDrawer,
  onSignOutFailed,
  onShowVersions,
}: {
  drawerOpen: boolean;
  onOpenDrawer: () => void;
  onSignOutFailed: () => void;
  onShowVersions: () => void;
}) {
  return (
    <header className={styles.bar}>
      <IconButton
        icon={MenuIcon}
        size={36}
        aria-label="Open navigation"
        aria-expanded={drawerOpen}
        aria-controls="drawer-sidebar"
        onClick={onOpenDrawer}
      />
      <Link to="/" className={styles.mark}>
        <LogoMark size={32} />
        <Wordmark />
      </Link>
      <span className={styles.end}>
        <AccountMenu
          variant="topbar"
          onSignOutFailed={onSignOutFailed}
          onShowVersions={onShowVersions}
        />
      </span>
    </header>
  );
}
