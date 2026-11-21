import { createFileRoute, redirect } from "@tanstack/react-router";
import { NotFound } from "@/components/NotFound";
import { Skeleton } from "@/components/states";
import styles from "@/features/app/AppLayout.module.css";
import { AppShell } from "@/features/shell/AppShell";

// The authenticated pathless layout (D-UI-08-11, UI-SPEC 7.8). It restores the session from the
// refresh cookie before anything renders, so a signed-in reload never flashes /signin; without
// one it redirects to /signin and remembers where the user was heading. Signed in, it renders the
// app shell (top bar, sidebar, main) around every child route.
export const Route = createFileRoute("/_app")({
  beforeLoad: async ({ context, location }) => {
    if (!(await context.session.restore())) {
      throw redirect({ to: "/signin", search: { redirect: location.href } });
    }
  },
  // Show the skeleton at once rather than after the router's one second default.
  pendingMs: 0,
  pendingMinMs: 0,
  pendingComponent: RestoringSession,
  component: AppShell,
  notFoundComponent: () => <NotFound unknownUrl />,
});

function RestoringSession() {
  return (
    <div className={styles.content}>
      <Skeleton label="session" rows={4} />
    </div>
  );
}
