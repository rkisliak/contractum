import { createFileRoute, redirect } from "@tanstack/react-router";
import type { Notice } from "@/auth/notices";
import { RotatePage } from "@/features/signin/RotatePage";

export type RotateSearch = { redirect?: string };

const RESTART: Notice = "rotation-restart";

// The forced bootstrap rotation (UI-SPEC 7.3, D-UI-08-15). The restricted token lives only in
// memory, so a reload (or a direct visit) has nothing to rotate with: back to sign-in.
export const Route = createFileRoute("/signin_/rotate")({
  validateSearch: (search: Record<string, unknown>): RotateSearch =>
    typeof search.redirect === "string"
      ? { redirect: search.redirect.slice(0, 2048) }
      : {},
  beforeLoad: ({ context, search }) => {
    if (context.session.restrictedToken() === null) {
      throw redirect({
        to: "/signin",
        search: {
          notice: RESTART,
          ...(search.redirect === undefined
            ? {}
            : { redirect: search.redirect }),
        },
      });
    }
  },
  component: function RotateRoute() {
    const { session } = Route.useRouteContext();
    const { redirect: target } = Route.useSearch();
    return <RotatePage session={session} redirect={target} />;
  },
});
