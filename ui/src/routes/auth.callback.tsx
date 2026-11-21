import { createFileRoute } from "@tanstack/react-router";
import {
  CallbackPage,
  type CallbackSearch,
} from "@/features/signin/CallbackPage";

// The Entra redirect landing (UI-SPEC 7.2, A-4): `?start=1` begins the MSAL redirect from here so
// the bridge page returns here, where the response is completed.
export const Route = createFileRoute("/auth/callback")({
  validateSearch: (search: Record<string, unknown>): CallbackSearch => ({
    ...(search.start === "1" || search.start === 1 ? { start: "1" } : {}),
    ...(typeof search.redirect === "string"
      ? { redirect: search.redirect.slice(0, 2048) }
      : {}),
  }),
  component: function CallbackRoute() {
    const search = Route.useSearch();
    return <CallbackPage search={search} />;
  },
});
