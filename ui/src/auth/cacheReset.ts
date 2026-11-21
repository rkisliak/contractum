// Server data cached for one user must never be shown to the next. Whenever the session starts
// or ends (sign-in, sign-out, expiry, sign-out in another tab), every cached query is dropped, so
// the next user's screens fetch with their own token. A token refresh for the same session keeps
// the cache.
import type { QueryClient } from "@tanstack/react-query";
import { tokenStore } from "./tokenStore";

export function clearCacheOnSessionChange(
  queryClient: QueryClient,
): () => void {
  let signedIn = tokenStore.get() !== null;
  return tokenStore.subscribe(() => {
    const now = tokenStore.get() !== null;
    if (now === signedIn) return;
    signedIn = now;
    void queryClient.cancelQueries();
    queryClient.clear();
  });
}
