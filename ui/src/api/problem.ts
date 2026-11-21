// RFC 9457 problem details as the UI sees them (D-UI-08-24, T-8-18). Every non-2xx response is
// reduced to the typed `Problem` below. Only the RFC 9457 members the UI renders (`type`, `title`,
// `detail`, `errors[{pointer, detail}]`) and the `X-Request-ID` header are ever read; a non-JSON
// body, an unknown member or a stack trace never reaches a field, so raw server text cannot be
// shown by accident.

export type ProblemFieldError = { pointer: string; detail: string };

export type HttpProblem = {
  kind: "problem";
  status: number;
  /** The `<slug>` of `urn:dc:problem:<slug>`; null for `about:blank` or any other type. */
  slug: string | null;
  title: string;
  detail: string | null;
  errors: ProblemFieldError[];
  requestId: string | null;
  /**
   * The problem's extension members (`report`, `run_id`, `referrers`, `reasons`, `highest_semver`,
   * `blockers`, `expected_odcs_id`, ...), unvalidated. Screens read the members they know through typed guards and never render
   * this object as raw text.
   */
  extensions?: Record<string, unknown>;
};

/** A rejected fetch: the server was never reached, so there is no status or request id. */
export type NetworkProblem = { kind: "network"; status: 0; requestId: null };

export type Problem = HttpProblem | NetworkProblem;

export type ProblemContext = "signin" | "action" | "page";

const SLUG_PREFIX = "urn:dc:problem:";
const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const REQUEST_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;
// Bounds on server-supplied text. It is rendered as React text nodes only, so length is the only
// remaining concern (a runaway body must not blow up the layout).
const MAX_TEXT = 2000;
const MAX_ERRORS = 200;

export const GENERIC_TITLE = "Something went wrong";

export function networkProblem(): NetworkProblem {
  return { kind: "network", status: 0, requestId: null };
}

export class ApiError extends Error {
  readonly problem: Problem;

  constructor(problem: Problem) {
    super(
      problem.kind === "network"
        ? "Network error"
        : `API error ${String(problem.status)}${problem.slug ? ` ${problem.slug}` : ""}`,
    );
    this.name = "ApiError";
    this.problem = problem;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

function fallbackTitle(status: number): string {
  if (status === 503) return "Service temporarily unavailable";
  if (status === 429) return "Too many requests";
  if (status === 404) return "Not found";
  return GENERIC_TITLE;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed.slice(0, MAX_TEXT) : null;
}

function slugOf(type: unknown): string | null {
  if (typeof type !== "string" || !type.startsWith(SLUG_PREFIX)) return null;
  const slug = type.slice(SLUG_PREFIX.length);
  return SLUG_PATTERN.test(slug) ? slug : null;
}

function errorsOf(value: unknown): ProblemFieldError[] {
  if (!Array.isArray(value)) return [];
  const out: ProblemFieldError[] = [];
  for (const entry of value.slice(0, MAX_ERRORS)) {
    if (!isRecord(entry)) continue;
    const pointer = typeof entry.pointer === "string" ? entry.pointer : null;
    const detail = text(entry.detail);
    if (pointer !== null && detail !== null) {
      out.push({ pointer: pointer.slice(0, MAX_TEXT), detail });
    }
  }
  return out;
}

// The extension members screens may read (allow-list: anything else in a body, such as a stack
// trace, is never copied).
const EXTENSION_MEMBERS = [
  "report",
  "run_id",
  "referrers",
  "hidden_referrer_count",
  "deleted_target_count",
  "reasons",
  "highest_semver",
  "existing_semver",
  "blockers",
  "expected_odcs_id",
  "found_odcs_id",
  "retry_after_s",
] as const;

function extensionsOf(
  body: Record<string, unknown>,
): Record<string, unknown> | undefined {
  const out: Record<string, unknown> = {};
  for (const key of EXTENSION_MEMBERS) {
    if (key in body) out[key] = body[key];
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

function requestIdOf(response: Response): string | null {
  const raw = response.headers.get("X-Request-ID")?.trim();
  return raw && REQUEST_ID_PATTERN.test(raw) ? raw : null;
}

export async function parseProblem(response: Response): Promise<HttpProblem> {
  const status = response.status;
  const base: HttpProblem = {
    kind: "problem",
    status,
    slug: null,
    title: fallbackTitle(status),
    detail: null,
    errors: [],
    requestId: requestIdOf(response),
  };
  const contentType = (
    response.headers.get("Content-Type") ?? ""
  ).toLowerCase();
  if (
    !contentType.includes("application/problem+json") &&
    !contentType.includes("application/json")
  ) {
    return base;
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return base;
  }
  if (!isRecord(body)) return base;
  return {
    ...base,
    slug: slugOf(body.type),
    title: text(body.title) ?? base.title,
    detail: text(body.detail),
    errors: errorsOf(body.errors),
    ...(extensionsOf(body) === undefined
      ? {}
      : { extensions: extensionsOf(body) }),
  };
}

const COPY_SIGNIN_INVALID = "Username or password is incorrect.";
const COPY_THROTTLED = "Too many attempts, try again in a moment.";
const COPY_FORBIDDEN_ACTION =
  "You are not permitted to do this here. Ask a folder admin for access.";
const COPY_UNAVAILABLE = "Service temporarily unavailable";
const COPY_NETWORK = "Can't reach the server";

/**
 * The fixed UI wording for a problem, from the Copywriting Contract, or null when no override
 * applies (the API's own title and detail are shown then). Keyed by slug and status.
 */
export function copyFor(
  problem: Problem,
  context: ProblemContext,
): string | null {
  if (problem.kind === "network") return COPY_NETWORK;
  const { status, slug } = problem;
  if (
    context === "signin" &&
    (slug === "invalid-credentials" || status === 401)
  ) {
    return COPY_SIGNIN_INVALID;
  }
  if (status === 429) return COPY_THROTTLED;
  if (status === 503) return COPY_UNAVAILABLE;
  if (status === 403 && context === "action") return COPY_FORBIDDEN_ACTION;
  return null;
}
