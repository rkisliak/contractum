// Entra sign-in with MSAL Browser v5 (D-UI-08-16, research Pattern 3, amendments A-3, A-4, A-7).
//
// Authorization code + PKCE through a full-page redirect (no popups). MSAL v5 sends every flow
// through a redirect bridge: the IdP returns to `/redirect.html` (a second Vite entry calling
// `broadcastResponseToMainFrame`), which caches the response and navigates back to the page that
// started the sign-in, `/auth/callback`. There `handleRedirectPromise` finishes the exchange.
//
// The resulting access token goes into the same in-memory `tokenStore` as a local sign-in, so the
// API client never knows which IdP is in use. MSAL keeps its own per-tab cache in sessionStorage
// (locked by D-UI-08-16). The library is loaded on demand, so a local-only deployment never
// downloads it.
import type {
  AuthenticationResult,
  Configuration,
  IPublicClientApplication,
} from "@azure/msal-browser";
import type { RuntimeConfig } from "@/config";
import { safeRedirect } from "./redirect";

export const CALLBACK_PATH = "/auth/callback";
export const BRIDGE_PATH = "/redirect.html";

export type EntraToken = { accessToken: string; expiresInS: number };

export function buildMsalConfig(
  config: RuntimeConfig,
  origin: string = window.location.origin,
): Configuration {
  const oidc = config.entraProtocol === "oidc";
  return {
    auth: {
      clientId: config.entraClientId,
      authority: config.entraAuthority,
      // OIDC mode (the compose mock): MSAL trusts only the listed authority host.
      knownAuthorities: oidc ? [new URL(config.entraAuthority).host] : [],
      redirectUri: `${origin}${BRIDGE_PATH}`,
      postLogoutRedirectUri: `${origin}/signin`,
    },
    cache: { cacheLocation: "sessionStorage" },
    system: { protocolMode: oidc ? "OIDC" : "AAD" },
  };
}

let instance: Promise<IPublicClientApplication> | null = null;

/** The one initialized client for this page (memoised). */
export function getMsal(
  config: RuntimeConfig,
): Promise<IPublicClientApplication> {
  instance ??= import("@azure/msal-browser").then(async (msal) => {
    const client = new msal.PublicClientApplication(buildMsalConfig(config));
    await client.initialize();
    return client;
  });
  return instance;
}

/** Test seam: forget the memoised client. */
export function resetMsalForTests(): void {
  instance = null;
}

function tokenOf(result: AuthenticationResult, now: number): EntraToken {
  const expiresAt = result.expiresOn?.getTime() ?? now + 300_000;
  return {
    accessToken: result.accessToken,
    expiresInS: Math.max(1, Math.floor((expiresAt - now) / 1000)),
  };
}

/** Starts the redirect to the IdP; the bridge brings the browser back to /auth/callback. */
export async function startEntraSignIn(
  config: RuntimeConfig,
  redirect?: string,
): Promise<void> {
  const client = await getMsal(config);
  // MSAL clears a finished redirect's "interaction in progress" marker (sessionStorage) only in
  // handleRedirectPromise. Microsoft's logout returns to /signin, which never calls it, so the
  // sign-out marker stayed and the next loginRedirect failed at once with interaction_in_progress.
  // Settling it here first is a no-op when nothing is pending (no response is in this URL).
  await client
    .handleRedirectPromise({ navigateToLoginRequestUrl: false })
    .catch(() => null);
  await client.loginRedirect({
    scopes: config.entraScopes,
    redirectStartPage: `${window.location.origin}${CALLBACK_PATH}`,
    // Carried through the IdP and checked again with safeRedirect on the way back (T-8-43).
    state: safeRedirect(redirect),
  });
}

export type EntraCompletion = EntraToken & { redirect: string };

/**
 * Finishes a redirect sign-in on /auth/callback. Resolves null when no response is pending (the
 * page was opened directly), and rejects with MSAL's error when the IdP refused.
 */
export async function completeEntraSignIn(
  config: RuntimeConfig,
  now: () => number = Date.now,
): Promise<EntraCompletion | null> {
  const client = await getMsal(config);
  const result = await client.handleRedirectPromise({
    navigateToLoginRequestUrl: false,
  });
  if (result === null) return null;
  client.setActiveAccount(result.account);
  // Some IdPs return no access token for OIDC-only scopes; renew silently in that case.
  const token =
    result.accessToken !== ""
      ? tokenOf(result, now())
      : tokenOf(
          await client.acquireTokenSilent({
            scopes: config.entraScopes,
            account: result.account,
          }),
          now(),
        );
  return { ...token, redirect: safeRedirect(result.state) };
}

/** True when MSAL's per-tab cache holds an account (a reload of an Entra session). */
export async function hasEntraAccount(config: RuntimeConfig): Promise<boolean> {
  const client = await getMsal(config);
  return client.getAllAccounts().length > 0;
}

function isInteractionRequired(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const named = error as { name?: unknown; errorCode?: unknown };
  return (
    named.name === "InteractionRequiredAuthError" ||
    (typeof named.errorCode === "string" &&
      /interaction_required|login_required|consent_required|no_tokens_found|refresh_token_expired/.test(
        named.errorCode,
      ))
  );
}

/**
 * A fresh access token without user interaction, or null when the IdP needs the user again
 * (no account, or MSAL reports interaction required). Other failures reject.
 */
export async function acquireEntraToken(
  config: RuntimeConfig,
  now: () => number = Date.now,
): Promise<EntraToken | null> {
  const client = await getMsal(config);
  const account = client.getActiveAccount() ?? client.getAllAccounts()[0];
  if (!account) return null;
  client.setActiveAccount(account);
  try {
    const result = await client.acquireTokenSilent({
      scopes: config.entraScopes,
      account,
    });
    return tokenOf(result, now());
  } catch (error) {
    if (isInteractionRequired(error)) return null;
    throw error;
  }
}

/** Ends the Entra session: MSAL's cache is cleared and the IdP signs out, back to /signin. */
export async function entraSignOut(config: RuntimeConfig): Promise<void> {
  const client = await getMsal(config);
  const account = client.getActiveAccount() ?? client.getAllAccounts()[0];
  try {
    await client.logoutRedirect({
      ...(account ? { account } : {}),
      postLogoutRedirectUri: `${window.location.origin}/signin`,
    });
  } catch {
    // An IdP without an end-session endpoint: forget the tab's MSAL cache at least.
    await client.clearCache();
  }
}
