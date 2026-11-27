// Server data cached for one user must never be shown to the next. Whenever the session starts
// or ends (sign-in, sign-out, expiry, sign-out in another tab) or the access token starts naming
// a different principal (another issuer or subject, for example a sign-in that replaces the token
// without a sign-out in between), every cached query is dropped, so the next user's screens fetch
// with their own token. A token refresh for the same principal keeps the cache (D-05). It fails
// closed: a replaced token whose principal cannot be read (an opaque token) is unknown, so it drops
// the cache like a different principal.
import type { QueryClient } from "@tanstack/react-query";
import { tokenStore } from "./tokenStore";

const decoder = new TextDecoder("utf-8", { fatal: true });

/**
 * A partition key for the query cache: the issuer and subject the access token claims, or null
 * when it is not a readable JWT. The payload is read without verifying the signature and the
 * token is never stored: a forged payload can at most clear the cache more often, and the API
 * verifies every token.
 */
export function principalOf(token: string): string | null {
  try {
    const segments = token.split(".");
    const payload = segments[1];
    if (segments.length !== 3 || !payload) return null;
    const base64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
    const bytes = Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
    const claims: unknown = JSON.parse(decoder.decode(bytes));
    if (typeof claims !== "object" || claims === null) return null;
    const { iss, sub } = claims as { iss?: unknown; sub?: unknown };
    return typeof iss === "string" && typeof sub === "string"
      ? JSON.stringify([iss, sub])
      : null;
  } catch {
    return null;
  }
}

function tokenNow(): string | null {
  return tokenStore.get()?.token ?? null;
}

export function clearCacheOnSessionChange(
  queryClient: QueryClient,
): () => void {
  let signedIn = tokenStore.get() !== null;
  let token = tokenNow();
  let principal = token === null ? null : principalOf(token);
  return tokenStore.subscribe(() => {
    const now = tokenStore.get() !== null;
    const nowToken = tokenNow();
    const nowPrincipal = nowToken === null ? null : principalOf(nowToken);
    // Two readable principals are compared by principal (a refresh keeps the cache); when either
    // side cannot be read, any change of the token string is treated as a different principal.
    const unreadable = nowPrincipal === null || principal === null;
    const changed =
      now !== signedIn ||
      (now && (unreadable ? nowToken !== token : nowPrincipal !== principal));
    signedIn = now;
    token = nowToken;
    principal = nowPrincipal;
    if (!changed) return;
    void queryClient.cancelQueries();
    queryClient.clear();
  });
}
