// The paced admin-API client shared by the demo seed and the volume seed. Playwright's
// Node-side request client reaches the gateway by its compose name (see global-setup.ts).
//
// Pacing (quick 261120-6i0, D-03): at most `MAX_IN_FLIGHT` requests at once and `MAX_PER_SECOND`
// started per second, so a seed of thousands of writes stays under the gateway's `rl-default`
// (50 requests per second, burst 100, per IP). A 429 or 503 is retried up to `MAX_RETRIES` times,
// waiting for `Retry-After` when the server names it.
import type { APIRequestContext } from "@playwright/test";

export type Page_<T> = { items: T[]; next_cursor?: string | null };

const MAX_IN_FLIGHT = 6;
const MAX_PER_SECOND = 40;
const MAX_RETRIES = 5;
const MIN_GAP_MS = 1000 / MAX_PER_SECOND;

const sleep = (ms: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, ms));

// One gate per process: every Client shares the same budget (they all hit one gateway).
let inFlight = 0;
const waiting: (() => void)[] = [];
let nextStart = 0;

async function acquire(): Promise<void> {
  while (inFlight >= MAX_IN_FLIGHT)
    await new Promise<void>((resolve) => waiting.push(resolve));
  inFlight += 1;
  const now = Date.now();
  const start = Math.max(now, nextStart);
  nextStart = start + MIN_GAP_MS;
  if (start > now) await sleep(start - now);
}

function release(): void {
  inFlight -= 1;
  waiting.shift()?.();
}

export type CallOptions = {
  json?: unknown;
  yaml?: string;
  headers?: Record<string, string>;
  /** A 409 (already there) is not an error. */
  allowConflict?: boolean;
  /** Statuses that are returned instead of thrown (a refusal is the point). */
  accept?: number[];
  /** Overrides the client's bearer (a persona's own token). */
  token?: string;
};

export type CallResult = { body: unknown; etag: string; status: number };

export class Client {
  constructor(
    private readonly ctx: APIRequestContext,
    private readonly token: string,
    private readonly label = "seed",
  ) {}

  async call(
    method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
    path: string,
    options: CallOptions = {},
  ): Promise<CallResult> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${options.token ?? this.token}`,
      ...options.headers,
    };
    let data: string | undefined;
    if (options.yaml !== undefined) {
      headers["Content-Type"] = "application/yaml";
      data = options.yaml;
    } else if (options.json !== undefined) {
      headers["Content-Type"] = "application/json";
      data = JSON.stringify(options.json);
    }
    for (let attempt = 0; ; attempt++) {
      await acquire();
      let res;
      try {
        res = await this.ctx.fetch(`/api/v1/${path}`, {
          method,
          headers,
          ...(data === undefined ? {} : { data }),
        });
      } finally {
        release();
      }
      const status = res.status();
      if ((status === 429 || status === 503) && attempt < MAX_RETRIES) {
        const after = Number(res.headers()["retry-after"]);
        await sleep(Number.isFinite(after) && after > 0 ? after * 1000 : 1000);
        continue;
      }
      if (options.allowConflict && status === 409)
        return { body: {}, etag: "", status };
      if (!res.ok() && !options.accept?.includes(status))
        throw new Error(
          `${this.label}: ${method} ${path} ${String(status)}: ${(await res.text()).slice(0, 600)}`,
        );
      const text = await res.text();
      let body: unknown = {};
      if (text) {
        try {
          body = JSON.parse(text) as unknown;
        } catch {
          body = { text };
        }
      }
      return { body, etag: res.headers()["etag"] ?? "", status };
    }
  }

  async get<T>(path: string): Promise<T> {
    return (await this.call("GET", path)).body as T;
  }

  async all<T>(path: string): Promise<T[]> {
    const out: T[] = [];
    let cursor: string | null | undefined = null;
    do {
      const sep = path.includes("?") ? "&" : "?";
      const page: Page_<T> = await this.get<Page_<T>>(
        `${path}${sep}limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      );
      out.push(...page.items);
      cursor = page.next_cursor;
    } while (cursor);
    return out;
  }

  post(path: string, options?: CallOptions) {
    return this.call("POST", path, options);
  }

  put(path: string, options?: CallOptions) {
    return this.call("PUT", path, options);
  }

  patch(path: string, options?: CallOptions) {
    return this.call("PATCH", path, options);
  }
}
