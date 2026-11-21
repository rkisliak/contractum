// The signed-in frame (Enterprise preview layout): skip link, the slate icon rail and the white
// navigation panel (mark, search, sections; the account menu is the rail's avatar) at 1024px and up, the panel
// hideable from the rail (remembered), a compact top bar with a start drawer below 1024px, one
// <main> on the page surface, and route-change focus: after a navigation focus moves to the page
// h1 (or main) and the new title is announced.
import { Outlet, useRouterState } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { Alert } from "@/components/Alert";
import { Drawer } from "@/components/Drawer";
import { TooltipProvider } from "@/components/Tooltip";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { cx } from "@/lib/cx";
import { AnnouncementBanner } from "./AnnouncementBanner";
import { Rail } from "./Rail";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";
import { VersionsDialog } from "./VersionsDialog";
import styles from "./AppShell.module.css";

const COLLAPSED_KEY = "dc-sidebar-collapsed";

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeCollapsed(value: boolean): void {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, value ? "1" : "0");
  } catch {
    // Storage blocked (private window): the preference simply is not remembered.
  }
}

export function AppShell() {
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  });
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [signOutFailed, setSignOutFailed] = useState(false);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const showVersions = () => {
    setVersionsOpen(true);
  };
  const [announcement, setAnnouncement] = useState("");
  const mainRef = useRef<HTMLElement>(null);
  const firstPath = useRef(pathname);

  // The drawer closes on route change (state adjusted during render, not in an effect).
  const [drawerPath, setDrawerPath] = useState(pathname);
  if (drawerPath !== pathname) {
    setDrawerPath(pathname);
    setDrawerOpen(false);
  }

  useEffect(() => {
    if (firstPath.current === pathname) return;
    firstPath.current = pathname;
    let frames = 0;
    let handle = 0;
    // The h1 may arrive a few frames later (after the route's data); fall back to main.
    const settle = () => {
      const heading = mainRef.current?.querySelector<HTMLElement>(
        "[data-page-heading]",
      );
      if (heading === null && frames < 20) {
        frames += 1;
        handle = window.requestAnimationFrame(settle);
        return;
      }
      (heading ?? mainRef.current)?.focus({ preventScroll: false });
      setAnnouncement(document.title);
    };
    handle = window.requestAnimationFrame(settle);
    return () => {
      window.cancelAnimationFrame(handle);
    };
  }, [pathname]);

  function toggleCollapsed() {
    setCollapsed((current) => {
      writeCollapsed(!current);
      return !current;
    });
  }

  return (
    <TooltipProvider>
      <div className={styles.shell}>
        <a href="#main-content" className={styles.skipLink}>
          Skip to content
        </a>
        <TopBar
          drawerOpen={drawerOpen}
          onOpenDrawer={() => {
            setDrawerOpen(true);
          }}
          onSignOutFailed={() => {
            setSignOutFailed(true);
          }}
          onShowVersions={showVersions}
        />
        <div className={cx(styles.body, collapsed && styles.bodyCollapsed)}>
          <div className={styles.railSlot}>
            <Rail
              panelOpen={!collapsed}
              onTogglePanel={toggleCollapsed}
              onSignOutFailed={() => {
                setSignOutFailed(true);
              }}
              onShowVersions={showVersions}
            />
          </div>
          {collapsed ? null : (
            <div className={styles.sidebarSlot} id="primary-panel">
              <Sidebar id="primary-sidebar" />
            </div>
          )}
          <main
            id="main-content"
            ref={mainRef}
            tabIndex={-1}
            className={styles.main}
          >
            <div className={styles.content} data-page-content="">
              {signOutFailed ? (
                <Alert
                  intent="danger"
                  title="Sign-out did not complete"
                  onDismiss={() => {
                    setSignOutFailed(false);
                  }}
                >
                  Your session is still active. Try signing out again.
                </Alert>
              ) : null}
              <AnnouncementBanner />
              <Outlet />
            </div>
          </main>
        </div>
        <Drawer
          open={drawerOpen}
          onOpenChange={setDrawerOpen}
          title="Navigation"
          side="start"
        >
          <Sidebar
            id="drawer-sidebar"
            onNavigate={() => {
              setDrawerOpen(false);
            }}
          />
        </Drawer>
        <VersionsDialog open={versionsOpen} onOpenChange={setVersionsOpen} />
        <VisuallyHidden aria-live="polite">{announcement}</VisuallyHidden>
      </div>
    </TooltipProvider>
  );
}
