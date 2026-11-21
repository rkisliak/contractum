// The only way UI code calls the platform (UI-06, D-UI-08-30): openapi-fetch typed with the paths
// generated from the merged OpenAPI document, so a route or shape that no longer exists fails
// type checking. Every non-2xx response and every rejected fetch surfaces as an ApiError carrying
// a typed RFC 9457 Problem (D-UI-08-24). Every request carries the bearer from the in-memory token
// store (D-UI-08-12). `installAuthRetry` (called by the session) adds the single retry after a 401.
import createClient, { type Middleware } from "openapi-fetch";
import { tokenStore } from "@/auth/tokenStore";
import type { RuntimeConfig } from "@/config";
import { trimCharEnd } from "@/lib/text";
import { ApiError, networkProblem, parseProblem } from "./problem";
import type { paths } from "./schema.gen";

export type ApiClient = ReturnType<typeof createClient<paths>>;

// openapi-fetch hands its fetch a ready Request; the platform `fetch` is assignable to this.
export type ApiClientOptions = {
  fetch?: (input: Request) => Promise<Response>;
};

// The merged document's paths already begin with /api/v1 (its servers entry is "/"), so the
// client base is apiBase without that trailing segment: "/api/v1" gives "" and a deployment
// under a sub-path such as "/dc/api/v1" gives "/dc".
const API_PREFIX = "/api/v1";

export function baseUrlFor(apiBase: string): string {
  const trimmed = trimCharEnd(apiBase, "/");
  return trimmed.endsWith(API_PREFIX)
    ? trimmed.slice(0, -API_PREFIX.length)
    : trimmed;
}

// A cancelled request (AbortController, TanStack Query unmount) is not a network failure.
function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

// Injects the held access token unless the caller set its own Authorization (the restricted
// password-rotation token does). Nothing is sent when no token is held, so login and refresh stay
// anonymous.
function withBearer(request: Request): Request {
  const held = tokenStore.get();
  if (held !== null && !request.headers.has("Authorization")) {
    request.headers.set("Authorization", `Bearer ${held.token}`);
  }
  return request;
}

const problemMiddleware: Middleware = {
  async onResponse({ response }) {
    if (!response.ok) {
      throw new ApiError(await parseProblem(response));
    }
    return undefined;
  },
};

export function createApiClient(
  config: RuntimeConfig,
  options: ApiClientOptions = {},
): ApiClient {
  const send =
    options.fetch ?? ((request: Request) => globalThis.fetch(request));
  const client = createClient<paths>({
    baseUrl: baseUrlFor(config.apiBase),
    // A rejected fetch (offline, DNS, TLS, connection reset) becomes a network Problem.
    fetch: async (request) => {
      try {
        return await send(withBearer(request));
      } catch (error) {
        if (isAbort(error)) throw error;
        throw new ApiError(networkProblem());
      }
    },
  });
  client.use(problemMiddleware);
  return client;
}

export type AuthRetryHandlers = {
  /** Refreshes the session once (shared, locked); false when there is no session to refresh. */
  refresh: () => Promise<boolean>;
  /** The session is over: clear it and send the user to /signin. */
  expire: () => void;
};

// Identity's own endpoints (login, refresh, logout, password) never get the retry: a 401 there is
// the answer, and retrying a refresh through a refresh would loop (T-8-27).
const AUTH_PATH_PREFIX = "/api/v1/identity/auth/";

function isAuthPath(request: Request): boolean {
  return new URL(request.url).pathname.includes(AUTH_PATH_PREFIX);
}

/**
 * One retry after a 401 (D-UI-08-11): refresh through the session's single flight, then replay the
 * request once with the new bearer. A second 401, or a refresh that finds no session, expires the
 * session. A request that carried its own Authorization (the restricted rotation token) is never
 * retried, and neither is anything under `/api/v1/identity/auth/`. Registered after the problem
 * middleware, so it sees the raw response first (openapi-fetch runs `onResponse` in reverse).
 * Returns the function that removes it again.
 */
export function installAuthRetry(
  api: ApiClient,
  handlers: AuthRetryHandlers,
): () => void {
  // A body is single use, so an unsent copy is taken before the first send.
  const unsent = new WeakMap<Request, Request>();
  const middleware: Middleware = {
    onRequest({ request }) {
      if (!isAuthPath(request) && !request.headers.has("Authorization")) {
        unsent.set(request, request.clone());
      }
      return undefined;
    },
    async onResponse({ request, response, options }) {
      if (response.status !== 401) return undefined;
      const replay = unsent.get(request);
      if (replay === undefined) return undefined;
      unsent.delete(request);

      // `withBearer` stamped the token this attempt used onto the same Request object.
      const sent = request.headers.get("Authorization");
      const held = tokenStore.get()?.token ?? null;
      if (sent !== null && held === null) return undefined; // cleared meanwhile: already expiring
      // Another call may have refreshed while this one was in flight: then just replay.
      const alreadyRefreshed = held !== null && sent !== `Bearer ${held}`;
      if (!alreadyRefreshed && !(await handlers.refresh())) {
        handlers.expire();
        return undefined;
      }
      const retried = await options.fetch(replay);
      if (retried.status === 401) handlers.expire();
      return retried;
    },
  };
  api.use(middleware);
  return () => {
    api.eject(middleware);
  };
}
