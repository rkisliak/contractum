import { createFileRoute, redirect } from "@tanstack/react-router";
import { isNotice, type Notice } from "@/auth/notices";
import { safeRedirect } from "@/auth/redirect";
import { tokenStore } from "@/auth/tokenStore";
import { SignInPage } from "@/features/signin/SignInPage";

export type SignInSearch = { redirect?: string; notice?: Notice };

// The search is untrusted input: only strings are kept. `notice` must be one of the enum values.
export function validateSignInSearch(
  search: Record<string, unknown>,
): SignInSearch {
  return {
    ...(typeof search.redirect === "string"
      ? { redirect: search.redirect }
      : {}),
    ...(isNotice(search.notice) ? { notice: search.notice } : {}),
  };
}

export const Route = createFileRoute("/signin")({
  validateSearch: validateSignInSearch,
  // A signed-in user has no business here (UI-SPEC 7.1): send them where they were heading. A
  // notice means the session was just ended on purpose, so the cookie is not probed again.
  beforeLoad: async ({ context, search }) => {
    const signedIn =
      search.notice === undefined
        ? await context.session.restore().catch(() => false)
        : tokenStore.get() !== null;
    if (signedIn) {
      throw redirect({ href: safeRedirect(search.redirect) });
    }
  },
  component: SignInRoute,
});

function SignInRoute() {
  const { session, config } = Route.useRouteContext();
  const { redirect, notice } = Route.useSearch();
  return (
    <SignInPage
      session={session}
      redirect={redirect}
      notice={notice}
      methods={{ local: config.localLoginEnabled, entra: config.entraEnabled }}
    />
  );
}
