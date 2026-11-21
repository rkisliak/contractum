// The browser session for a local account (D-UI-08-11, D-UI-08-12, D-UI-08-14, research Pattern 7).
//
// Sign-in sends `X-Token-Delivery: cookie`, so the refresh token arrives only as the HttpOnly
// `__Secure-dc_refresh` cookie and the body carries just the short-lived access token, which goes
// into the in-memory `tokenStore`. A reload restores the session by posting the cookie back to
// the refresh route.
//
// Refresh is single flight in this tab and runs inside the `dc-refresh` Web Lock across tabs: the
// refresh token is one-time use, and presenting a rotated one revokes its whole family. A 401 on
// any API call refreshes once and retries once (see `installAuthRetry`), a proactive refresh runs
// at 80% of the token's life, and sign-out or expiry sends every tab to /signin with a notice.
import { installAuthRetry, type ApiClient } from "@/api/client";
import { isApiError } from "@/api/problem";
import { withLock, type LockRunner } from "./locks";
import type { Notice } from "./notices";
import { tokenStore } from "./tokenStore";

const COOKIE_DELIVERY = { "X-Token-Delivery": "cookie" } as const;
const ROTATE_SCOPE = "password:rotate";
// One name across every tab of the origin: two tabs never present the same one-time cookie.
const REFRESH_LOCK = "dc-refresh";
const CHANNEL_NAME = "dc-session";
const LOGOUT_MESSAGE = "logout";
const PROACTIVE_FRACTION = 0.8;

export type SignInResult =
  { kind: "signed-in" } | { kind: "rotation-required" };

/** Navigates the router to the sign-in screen; expiry and cross-tab sign-out use it. */
export type SessionNavigate = (
  to: "/signin",
  search: { redirect?: string; notice?: Notice },
) => void;

/** The slice of BroadcastChannel the session uses, so a test can inject a pair. */
export type SessionChannel = {
  postMessage: (message: unknown) => void;
  close: () => void;
  addEventListener: (
    type: "message",
    listener: (event: MessageEvent) => void,
  ) => void;
};

/**
 * The Entra side of the session (D-UI-08-16), given only when Entra sign-in is enabled. The
 * session stays IdP-agnostic: an Entra token lands in the same store, renews silently through
 * MSAL instead of the cookie, and signs out through the IdP.
 */
export type EntraAdapter = {
  /** MSAL's per-tab cache holds an account (a reload of an Entra session). */
  hasAccount: () => Promise<boolean>;
  /** A fresh token without interaction, or null when the user must sign in again. */
  acquire: () => Promise<{ accessToken: string; expiresInS: number } | null>;
  /** Clears MSAL's cache and signs out at the IdP (navigates away). */
  signOut: () => Promise<void>;
};

export type SessionIdp = "local" | "entra";

export type SessionOptions = {
  navigate: SessionNavigate;
  /** Entra sign-in support; omitted when the deployment has Entra off. */
  entra?: EntraAdapter | null;
  /** Cross-tab lock runner; defaults to Web Locks with an in-tab fallback. */
  withLock?: LockRunner;
  /** Cross-tab channel; defaults to `BroadcastChannel("dc-session")` when the browser has one. */
  channel?: SessionChannel | null;
  /** Clock in epoch milliseconds, for the proactive refresh delay. */
  now?: () => number;
  /** The path the user is on (path, search, hash); defaults to `window.location`. */
  currentPath?: () => string;
};

export type Session = {
  signIn: (username: string, password: string) => Promise<SignInResult>;
  /** Adopts the access token of a completed Entra redirect sign-in. */
  adoptEntraToken: (accessToken: string, expiresInS: number) => void;
  /** Which IdP signed this page's session in, or null without a session. */
  idp: () => SessionIdp | null;
  /** Exchanges the refresh cookie for an access token: true on 200, false on 401. */
  refresh: () => Promise<boolean>;
  /** True when a token is held, otherwise tries the cookie refresh. */
  restore: () => Promise<boolean>;
  /**
   * Ends the session: revokes the refresh family on the cookie path, drops the in-memory token,
   * tells the other tabs and goes to /signin. A failed logout call rejects and changes nothing,
   * so the user is never told they are signed out while the cookie is still good.
   */
  signOut: () => Promise<void>;
  /** The session is dead (refresh refused): clear it and go to /signin with the current path. */
  expire: () => void;
  /**
   * The server already ended the session on purpose (a password change revoked every session):
   * drop the token and go to /signin with this notice. A request still in flight that then fails
   * with 401 no longer expires the session, so its session-expired notice cannot replace this one.
   */
  end: (notice: Notice) => void;
  /** The restricted (password:rotate) token from a bootstrap sign-in, kept apart from the store. */
  restrictedToken: () => string | null;
  clearRestrictedToken: () => void;
  /**
   * Completes a forced rotation (UI-SPEC 7.3): posts the new password with the restricted token,
   * drops that token, then signs in again on the cookie path with the same username.
   */
  rotate: (newPassword: string) => Promise<SignInResult>;
  redirectToSignIn: (redirect?: string, notice?: Notice) => void;
  /** Stops the proactive timer and closes the channel. */
  dispose: () => void;
};

function defaultChannel(): SessionChannel | null {
  return typeof BroadcastChannel === "undefined"
    ? null
    : new BroadcastChannel(CHANNEL_NAME);
}

function defaultCurrentPath(): string {
  const { pathname, search, hash } = window.location;
  return `${pathname}${search}${hash}`;
}

export function createSession(
  api: ApiClient,
  options: SessionOptions,
): Session {
  const lock = options.withLock ?? withLock;
  const now = options.now ?? Date.now;
  const currentPath = options.currentPath ?? defaultCurrentPath;
  const channel =
    options.channel === undefined ? defaultChannel() : options.channel;

  // A restricted token may only rotate the password; it is never a session, so it never enters
  // the store that every API call reads.
  let restricted: string | null = null;
  let restrictedUsername: string | null = null;
  let inFlight: Promise<boolean> | null = null;
  let proactive: ReturnType<typeof setTimeout> | null = null;
  // The session-expired notice is for a user who had a session in this page's lifetime; after a
  // reload with a dead cookie nothing remembers one (no storage by design), so no notice.
  let hadSession = false;
  let expired = false;
  let idp: SessionIdp | null = null;
  const entra = options.entra ?? null;

  function cancelProactive(): void {
    if (proactive !== null) clearTimeout(proactive);
    proactive = null;
  }

  // A token the session did not store (another holder cleared it) must not keep a timer alive.
  const unsubscribe = tokenStore.subscribe(() => {
    if (tokenStore.get() === null) cancelProactive();
  });

  function store(
    accessToken: string,
    expiresInS: number,
    from: SessionIdp = "local",
  ): void {
    idp = from;
    tokenStore.set(accessToken, expiresInS);
    hadSession = true;
    expired = false;
    scheduleProactive();
  }

  function scheduleProactive(): void {
    cancelProactive();
    const held = tokenStore.get();
    if (held === null) return;
    const delay = Math.max(0, (held.expiresAt - now()) * PROACTIVE_FRACTION);
    proactive = setTimeout(() => {
      proactive = null;
      // A refresh refused here ends the session; a server or network failure leaves the still
      // valid token alone and the next 401 takes the retry path.
      void refresh()
        .then((ok) => {
          if (!ok) expire();
        })
        .catch(() => undefined);
    }, delay);
  }

  async function signIn(
    username: string,
    password: string,
  ): Promise<SignInResult> {
    const { data } = await api.POST("/api/v1/identity/auth/login", {
      params: { header: COOKIE_DELIVERY },
      body: { username, password },
    });
    if (data === undefined) {
      throw new Error("The login response had no body");
    }
    if (data.scope.split(" ").includes(ROTATE_SCOPE)) {
      restricted = data.access_token;
      restrictedUsername = username;
      return { kind: "rotation-required" };
    }
    restricted = null;
    restrictedUsername = null;
    store(data.access_token, data.expires_in);
    return { kind: "signed-in" };
  }

  async function rotate(newPassword: string): Promise<SignInResult> {
    const token = restricted;
    const username = restrictedUsername;
    if (token === null || username === null) {
      throw new Error("No restricted session to rotate");
    }
    // The caller-set Authorization keeps the restricted token out of the store and out of the
    // refresh-and-retry path.
    await api.POST("/api/v1/identity/auth/password", {
      headers: { Authorization: `Bearer ${token}` },
      body: { new_password: newPassword },
    });
    restricted = null;
    restrictedUsername = null;
    return signIn(username, newPassword);
  }

  async function postRefresh(): Promise<boolean> {
    try {
      const { data } = await api.POST("/api/v1/identity/auth/refresh", {
        params: { header: COOKIE_DELIVERY },
      });
      if (data === undefined) return false;
      store(data.access_token, data.expires_in);
      return true;
    } catch (error) {
      // No cookie, an expired or a revoked family: there is no session to restore.
      if (isApiError(error) && error.problem.status === 401) {
        tokenStore.clear();
        return false;
      }
      throw error;
    }
  }

  async function entraRefresh(): Promise<boolean> {
    if (entra === null) return false;
    const token = await entra.acquire();
    if (token === null) {
      tokenStore.clear();
      return false;
    }
    store(token.accessToken, token.expiresInS, "entra");
    return true;
  }

  function refresh(): Promise<boolean> {
    // An Entra session renews through MSAL; the refresh cookie belongs to local sign-in only.
    if (idp === "entra") {
      inFlight ??= entraRefresh().finally(() => {
        inFlight = null;
      });
      return inFlight;
    }
    // Single flight inside the tab, then the lock across tabs. The request is built after the lock
    // is granted, so the browser attaches the cookie another tab may just have rotated; a token
    // read before the wait is never reused.
    inFlight ??= lock(REFRESH_LOCK, postRefresh).finally(() => {
      inFlight = null;
    });
    return inFlight;
  }

  async function restore(): Promise<boolean> {
    if (tokenStore.get() !== null) return true;
    // A reload of an Entra session: MSAL's per-tab cache still holds the account.
    if (entra !== null && (await entra.hasAccount().catch(() => false))) {
      idp = "entra";
      if (await refresh().catch(() => false)) return true;
      idp = null;
    }
    return refresh();
  }

  function redirectToSignIn(redirect?: string, notice?: Notice): void {
    options.navigate("/signin", {
      ...(redirect === undefined ? {} : { redirect }),
      ...(notice === undefined ? {} : { notice }),
    });
  }

  function expire(): void {
    // Two calls failing together expire once.
    if (expired) return;
    expired = true;
    tokenStore.clear();
    restricted = null;
    idp = null;
    const path = currentPath();
    redirectToSignIn(
      path.startsWith("/signin") ? undefined : path,
      hadSession ? "session-expired" : undefined,
    );
  }

  function end(notice: Notice): void {
    expired = true;
    hadSession = false;
    cancelProactive();
    tokenStore.clear();
    restricted = null;
    idp = null;
    redirectToSignIn(undefined, notice);
  }

  async function signOut(): Promise<void> {
    if (idp === "entra" && entra !== null) {
      // No refresh cookie to revoke: drop the token, tell the other tabs, sign out at the IdP
      // (MSAL navigates to the end-session endpoint, which returns to /signin).
      tokenStore.clear();
      restricted = null;
      hadSession = false;
      idp = null;
      channel?.postMessage(LOGOUT_MESSAGE);
      await entra.signOut();
      return;
    }
    await api.POST("/api/v1/identity/auth/logout", {
      params: { header: COOKIE_DELIVERY },
    });
    tokenStore.clear();
    restricted = null;
    hadSession = false;
    idp = null;
    channel?.postMessage(LOGOUT_MESSAGE);
    redirectToSignIn();
  }

  // Another tab signed out: its logout revoked the family, so this tab's token is dead weight.
  channel?.addEventListener("message", (event) => {
    if (event.data !== LOGOUT_MESSAGE) return;
    tokenStore.clear();
    restricted = null;
    hadSession = false;
    idp = null;
    redirectToSignIn(undefined, "signed-out-elsewhere");
  });

  installAuthRetry(api, { refresh, expire });

  return {
    signIn,
    adoptEntraToken: (accessToken, expiresInS) => {
      restricted = null;
      store(accessToken, expiresInS, "entra");
    },
    idp: () => (tokenStore.get() === null ? null : idp),
    refresh,
    restore,
    signOut,
    expire,
    end,
    restrictedToken: () => restricted,
    clearRestrictedToken: () => {
      restricted = null;
      restrictedUsername = null;
    },
    rotate,
    redirectToSignIn,
    dispose: () => {
      cancelProactive();
      unsubscribe();
      channel?.close();
    },
  };
}
